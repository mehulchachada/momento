"""Render the replay bar offscreen with a stubbed daemon and save screenshots.

    python3 -m unittest tests.test_overlay_offscreen

Screenshots land in /tmp/claude-1000/momento-{overlay,settings,controls,v3,v4,v5}-*.png
(override with $MOMENTO_SHOT_DIR). Each is the bar composited over a plain backdrop that
stands in for the game.
"""

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_WAYLAND_SHELL_INTEGRATION", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from momento import config, ipc, overlay, quality, settings  # noqa: E402

SHOT_DIR = Path(os.environ.get("MOMENTO_SHOT_DIR", "/tmp/claude-1000"))
REAL_REQUEST = ipc.request  # the resident bar's control socket is always reached for real

STATUS = {"ok": True, "state": "recording", "recording": True, "buffered": 754.0,
          "max_seconds": 3600, "source": "portal", "encoder": "vah264enc",
          "output_dir": "/home/user/Videos/Momento"}


DEVICES = {
    "outputs": [
        {"name": "ROG Ally.monitor", "label": "ROG Ally", "default": False},
        {"name": "alsa_output.pci-0000_09_00.1.hdmi-stereo.monitor",
         "label": "Radeon High Definition Audio Controller Digital Stereo (HDMI)", "default": False},
        {"name": "alsa_output.pci-0000_09_00.6.analog-stereo.monitor",
         "label": "Ryzen HD Audio Controller Analog Stereo", "default": True},
    ],
    "inputs": [
        {"name": "alsa_input.usb-Blue_Yeti.analog-stereo", "label": "Yeti Stereo Microphone", "default": True},
    ],
}


def settings_reply(devices=DEVICES, free=None, **values):
    cfg = config.load(Path("/nonexistent/momento-test.toml"))
    for k, v in values.items():
        for section, key, val in settings.writes(k, settings.normalize(k, v)):
            cfg[section][key] = val
    data = settings.describe(cfg, devices=devices)
    if free is not None:
        secs = data["max_seconds"]
        data["storage"] = {"free": free, "reclaimable": 0, "required": {
            f"{r}/{q}/{f}": quality.buffer_gb(quality.bitrate_kbps(
                {"resolution": r, "quality": q, "fps": f}), secs) * 1e9
            for r in quality.RESOLUTIONS for q in quality.QUALITIES for f in quality.FPS_CHOICES}}
        v = data["values"]
        data["storage"]["current"] = f"{v['resolution']}/{v['quality']}/{v['fps']}"
    return data


OK_STORAGE = {"ok": True, "free": 742e9, "reclaimable": 2.1e9, "required": 7.9e9,
              "path": "/home/user/.cache/momento"}
TIGHT_STORAGE = {**OK_STORAGE, "free": 11.3e9, "reclaimable": 0}

LOW_ERROR = "Not enough free space: needs 7.2 GB, 3.1 GB free"
LOW = {"ok": False, "free": 3.1e9, "reclaimable": 0, "required": 7.2e9, "path": "/home/user/.cache/momento"}


class FakeDaemon:
    def __init__(self, running=True, fail=False, devices=DEVICES, paused=False, extra=None, free=None,
                 resume_reply=None, values=None, configure_reply=None, storage=OK_STORAGE):
        self.running, self.fail, self.devices, self.paused = running, fail, devices, paused
        self.extra = dict(extra or {})   # merged into every status reply
        self.free = free                 # settings: free bytes for the storage check
        self.resume_reply = resume_reply
        self.values = dict(values or {})  # settings: current config values
        self.configure_reply = configure_reply
        self.storage = storage           # status: the storage block (None = an older daemon)
        self.stopped = False
        self.saves = []
        self.save_msgs = []
        self.configures = []
        self.controls = []

    def request(self, msg, timeout=120, **_):
        if not self.running:
            raise ipc.DaemonNotRunning("no socket")
        if msg["cmd"] == "status":
            st = dict(STATUS)
            if self.storage:
                st["storage"] = dict(self.storage)
            if self.stopped:
                st.update(state="stopped", recording=False, buffered=0)
            elif self.paused:
                st.update(state="paused", recording=False)
            st.update(self.extra)
            return st
        if msg["cmd"] == "settings":
            return settings_reply(self.devices, free=self.free, **self.values)
        if msg["cmd"] == "configure":
            self.configures.append(msg["changes"])
            time.sleep(0.3)
            if self.configure_reply:
                return self.configure_reply
            return {"ok": True, "changed": msg["changes"], "restarted": True, "paused": False}
        if msg["cmd"] == "pick_window":
            self.controls.append("pick_window")
            self.extra.update(state="starting", recording=False)
            return {"ok": True, "state": "starting"}
        if msg["cmd"] == "stop":
            # the service keeps running; recording stops and the history is cleared
            self.controls.append("stop")
            self.stopped, self.paused = True, False
            self.extra.pop("buffered", None)
            return {"ok": True, "state": "stopped", "buffer_cleared": True}
        if msg["cmd"] in ("pause", "resume", "quit"):
            self.controls.append(msg["cmd"])
            if msg["cmd"] == "resume" and self.resume_reply:
                return self.resume_reply
            if msg["cmd"] == "pause":
                self.paused = True
            elif msg["cmd"] == "resume":
                self.paused = self.stopped = False
            elif msg["cmd"] == "quit":
                self.running = False
            return {"ok": True, "state": "paused" if self.paused else "starting"}
        if msg["cmd"] == "save":
            self.saves.append(msg["seconds"])
            self.save_msgs.append(dict(msg))
            time.sleep(0.2)
            if self.fail:
                return {"ok": False, "error": "encoder stalled: no segments written"}
            secs = min(msg["seconds"], STATUS["buffered"])
            return {"ok": True, "path": "/home/user/Videos/Momento/Replay_2026-09-26_21-04-11_5m.mp4",
                    "seconds": secs, "requested": msg["seconds"], "partial": secs < msg["seconds"]}
        return {"ok": False, "error": "unknown"}


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class OverlayOffscreen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        cls._orig = ipc.request
        cls._tmp = tempfile.TemporaryDirectory()
        overlay.LAST_FILE = Path(cls._tmp.name) / "overlay.last"

    @classmethod
    def tearDownClass(cls):
        ipc.request = cls._orig
        cls._tmp.cleanup()

    def make(self, daemon):
        ipc.request = daemon.request
        Bar, fetch_status = overlay._build([])
        bar = Bar()
        bar.apply_status(fetch_status(timeout=1.0))
        bar.show()
        bar.focus_default()
        self.app.installEventFilter(bar)
        self.addCleanup(bar.close)
        self.addCleanup(self.app.removeEventFilter, bar)
        pump(self.app, 0.1)
        return bar

    def shot(self, bar, name, prefix="overlay"):
        bar.settle()  # finish pill transitions so the picture shows the end state
        img = bar.grab()
        canvas = QPixmap(img.width() + 80, img.height() + 60)
        canvas.fill(QColor("#4a5563"))
        p = QPainter(canvas)
        p.drawPixmap(QPoint(40, 20), img)
        p.end()
        canvas.save(str(SHOT_DIR / f"momento-{prefix}-{name}.png"))

    def wait_for(self, cond, timeout=3):
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            pump(self.app, 0.02)
        self.assertTrue(cond(), "condition not reached in time")

    def key(self, k):
        target = self.app.focusWidget() or self.app.activeWindow()
        self.app.sendEvent(target, QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))

    def wait_done(self, bar):
        deadline = time.monotonic() + 3
        while not bar.done and time.monotonic() < deadline:
            pump(self.app, 0.05)

    def test_bar_geometry(self):
        bar = self.make(FakeDaemon(True))
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)
        self.assertLess(bar.width(), 1000)
        for o in bar.options:
            self.assertGreaterEqual(o.width(), overlay.OPTION_MIN_WIDTH)

    def test_save_ends_clip_when_bar_opened_unless_excluded(self):
        # Desktop can't hide the bar from capture: the clip ends when the bar appeared.
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.assertIsNotNone(bar.shown_at)
        opened = bar.shown_at
        old = overlay.CAPTURE_EXCLUDED
        try:
            overlay.CAPTURE_EXCLUDED = False
            bar.choose(bar.options[1])
            self.wait_for(lambda: daemon.save_msgs)
            self.assertEqual(daemon.save_msgs[0].get("until"), opened)
        finally:
            overlay.CAPTURE_EXCLUDED = old
        # KWin hides the bar: the clip ends "now", no until.
        daemon2 = FakeDaemon(True)
        bar2 = self.make(daemon2)
        try:
            overlay.CAPTURE_EXCLUDED = True
            bar2.choose(bar2.options[1])
            self.wait_for(lambda: daemon2.save_msgs)
            self.assertNotIn("until", daemon2.save_msgs[0])
        finally:
            overlay.CAPTURE_EXCLUDED = old

    def test_kwin_exclusion_skipped_in_tests(self):
        self.assertFalse(overlay._exclude_from_capture())

    def test_normal_and_save(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.assertEqual(bar.stack.currentIndex(), 0)
        self.assertEqual(bar.time.text(), "12:34")
        # Default focus is 1m (index 2); 15m / 30m / 60m exceed 12:34 -> dimmed.
        self.assertTrue(bar.options[2].hasFocus())
        self.assertEqual([o.property("long") for o in bar.options],
                         [False] * 5 + [True] * 3)
        self.key(Qt.Key_Right)
        self.key(Qt.Key_Right)
        self.assertTrue(bar.options[4].hasFocus())
        self.key(Qt.Key_Left)
        self.key(Qt.Key_Right)
        # recording: just the logo, the red dot and the time -- no "Replay" label
        self.assertEqual((bar.view, bar.name.text()), ("rec", ""))
        self.assertTrue(bar.name.isHidden())
        self.assertIn(overlay.RED, bar.dot.styleSheet())
        self.assertTrue(bar.logo.isVisible())
        self.assertLess(bar.logo.mapTo(bar, bar.logo.rect().topLeft()).x(),
                        bar.dot.mapTo(bar, bar.dot.rect().topLeft()).x())
        self.assertEqual(bar.logo.width(), overlay.LOGO_SIZE)
        self.assertEqual((bar.storage_hint.level, bar.storage_hint.label), ("ok", "742 GB"))
        self.shot(bar, "normal")
        self.shot(bar, "clip", "v3")
        self.shot(bar, "clip-recording-green", "v4")

        h = bar.size()
        self.key(Qt.Key_Return)  # saves 5m
        pump(self.app, 0.05)
        self.assertTrue(bar.saving)
        self.assertEqual(bar.stack.currentIndex(), 1)
        self.assertEqual(bar.size(), h)
        self.assertIn("Saving last 5m", bar.line.text())
        self.shot(bar, "saving")
        self.wait_done(bar)
        self.assertTrue(bar.done)
        self.assertEqual(daemon.saves, [300])
        self.assertIn("Replay_2026-09-26_21-04-11_5m.mp4", bar.line.text())
        self.assertEqual(overlay._last_choice(), 300)
        self.assertEqual(bar.size(), h)          # the result line keeps the bar's size
        self.shot(bar, "saved")
        self.shot(bar, "saved", "v3")

    def test_number_key_and_error(self):
        daemon = FakeDaemon(True, fail=True)
        bar = self.make(daemon)
        self.key(Qt.Key_1)
        self.wait_done(bar)
        self.assertEqual(daemon.saves, [15])
        self.assertIn("encoder stalled", bar.line.text())
        self.shot(bar, "error")

    def assert_off(self, bar):
        self.assertEqual(bar.stack.currentIndex(), 0)          # the same clip bar, not a line page
        self.assertEqual((bar.view, bar.name.text(), bar.time.text()), ("off", "Off", "—"))
        self.assertTrue(all(o.visual_state == "disabled" for o in bar.options))
        self.assertTrue(all(not o.isEnabled() for o in bar.options))
        play = bar.controls[0]["pause"]
        self.assertEqual(play.kind, "start")
        self.assertTrue(play.isEnabled())
        self.assertFalse(bar.controls[0]["stop"].isEnabled())
        self.assertTrue(bar.gear.isEnabled())
        self.assertTrue(bar.hintbar.isHidden())

    def test_daemon_off(self):
        daemon = FakeDaemon(False)
        bar = self.make(daemon)
        self.assert_off(bar)
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)
        self.key(Qt.Key_3)  # ignored while off
        self.key(Qt.Key_Left)  # the disabled lengths are skipped
        self.assertTrue(bar.gear.hasFocus())
        self.key(Qt.Key_Right)
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.assertEqual(daemon.saves, [])
        self.shot(bar, "off")
        self.shot(bar, "off", "v3")
        self.shot(bar, "daemon-off", "v4")
        self.assertEqual(bar.width(), self.make(FakeDaemon(True)).width())  # same bar, same size

    # ---------------------------------------------------------------- gear / settings

    def open_settings(self, bar):
        self.key(Qt.Key_S)
        self.wait_for(lambda: bar.mode == "settings")
        pump(self.app, 0.05)

    def test_gear_reachable_past_60m(self):
        bar = self.make(FakeDaemon(True))
        bar.options[-1].setFocus()
        self.key(Qt.Key_Right)
        self.assertTrue(bar.gear.hasFocus())
        self.key(Qt.Key_Tab)
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.key(Qt.Key_Tab)
        self.assertTrue(bar.controls[0]["stop"].hasFocus())
        self.key(Qt.Key_Tab)  # wraps to 15s
        self.assertTrue(bar.options[0].hasFocus())
        self.key(Qt.Key_Left)
        self.key(Qt.Key_Left)
        self.key(Qt.Key_Left)
        self.assertTrue(bar.gear.hasFocus())
        self.shot(bar, "clip-gear-focus", "settings")
        bar.options[2].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "clip", "settings")

    def test_settings_keyboard_and_apply(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.move(100, 700)
        h0, bottom0 = bar.height(), bar.y() + bar.height()
        bar.gear.setFocus()
        self.key(Qt.Key_Return)  # activates the gear
        self.wait_for(lambda: bar.mode == "settings")
        self.assertGreater(bar.height(), h0 + 4 * overlay.ROW_H)
        self.assertEqual(bar.y() + bar.height(), bottom0)  # grew upward
        self.assertEqual(bar.stack.currentIndex(), 2)
        self.assertIn("60 fps", bar.foot.text())
        self.assertIn("6.8 GB for 60 min", bar.foot.text())
        self.assertTrue(bar.row("mic_device").isHidden())
        self.assertEqual({r.key: r.icon.kind for r in bar.rows},
                         {"record": "fullscreen", "resolution": "display", "fps": "gauge", "quality": "sliders",
                          "audio_source": "speaker", "mic": "mic", "mic_device": "micdev"})
        xs = {r.icon.mapTo(bar, r.icon.rect().topLeft()).x() for r in bar.visible_rows()}
        self.assertEqual(len(xs), 1)             # one icon column
        self.assertTrue(all(r.height() == overlay.ROW_H for r in bar.rows))
        self.assertEqual((bar.apply_btn.glyph, bar.back_btn.glyph), ("check", "back"))
        self.assertEqual(bar.rows[0].key, "record")                 # what to record comes first
        self.assertTrue(bar.row("record").buttons[0].hasFocus())    # Full screen
        self.key(Qt.Key_Down)
        self.assertTrue(bar.row("resolution").buttons[1].hasFocus())  # 1080p
        self.assertEqual([b.text() for b in bar.row("resolution").buttons],
                         ["720p", "1080p", "1440p", "4K", "Native"])
        self.key(Qt.Key_Right)                   # 1440p
        self.assertIn("11 GB", bar.foot.text().replace("10.8", "11"))
        self.key(Qt.Key_Down)                    # frame rate row (stays 60 fps)
        self.assertTrue(bar.row("fps").buttons[0].hasFocus())
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Right)                   # ultra
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Right)                   # first output
        self.key(Qt.Key_Down)
        h1 = bar.height()
        self.key(Qt.Key_Right)                   # mic on -> device row appears
        pump(self.app, 0.05)
        self.assertFalse(bar.row("mic_device").isHidden())
        self.assertEqual(bar.height(), h1 + overlay.ROW_H)
        self.assertEqual(bar.y() + bar.height(), bottom0)
        self.key(Qt.Key_Down)
        self.assertTrue(bar.row("mic_device").buttons[0].hasFocus())
        self.key(Qt.Key_Down)
        self.assertTrue(bar.apply_btn.hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "settings-apply", "v3")
        self.key(Qt.Key_Right)
        self.assertTrue(bar.back_btn.hasFocus())
        self.key(Qt.Key_Left)
        self.assertTrue(bar.apply_btn.hasFocus())
        self.key(Qt.Key_Up)
        self.key(Qt.Key_Up)
        self.key(Qt.Key_Up)                      # back on Sound
        self.assertTrue(bar.row("audio_source").buttons[1].hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "settings", "settings")
        self.shot(bar, "settings", "v3")
        self.key(Qt.Key_Return)                  # Enter applies from anywhere
        pump(self.app, 0.05)
        self.assertEqual(bar.apply_state, "busy")
        self.assertIn("Applying", bar.foot.text())
        self.shot(bar, "applying", "settings")
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"resolution": "1440p", "quality": "ultra",
                                              "audio_source": "ROG Ally.monitor", "mic": "on"}])
        self.assertIn("recording restarted", bar.foot.text())
        self.shot(bar, "saved", "settings")
        self.wait_for(lambda: bar.mode == "clip", timeout=overlay.APPLY_CLOSE_MS / 1000 + 2)
        self.assertEqual(bar.height(), h0)
        self.assertEqual(bar.y() + bar.height(), bottom0)

    def test_settings_click_esc_and_no_changes(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.open_settings(bar)
        self.assertIn("your replay is kept", bar.note.text())
        self.assertEqual(bar.idle.interval(), overlay.SETTINGS_IDLE_MS)
        bar.row("quality").buttons[0].click()   # touch / click
        self.assertEqual(bar.row("quality").value, "standard")
        self.assertEqual(bar.changes(), {"quality": "standard"})
        self.key(Qt.Key_Escape)                  # back to clips, nothing written
        self.assertEqual(bar.mode, "clip")
        self.assertEqual(bar.idle.interval(), overlay.IDLE_CLOSE_MS)
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)
        self.assertTrue(bar.gear.hasFocus())
        self.open_settings(bar)
        bar.apply_btn.click()                    # nothing changed -> just go back
        self.assertEqual(bar.mode, "clip")
        self.assertEqual(daemon.configures, [])
        # long device names are elided, not allowed to widen the bar
        self.open_settings(bar)
        hdmi = bar.row("audio_source").buttons[2]
        self.assertTrue(hdmi.text().endswith("…"))
        self.assertLessEqual(bar.row("audio_source").sizeHint().width(), bar.width())

    def test_settings_many_devices_cycle(self):
        outs = [{"name": f"out{i}.monitor", "label": f"Speaker {i}", "default": i == 0} for i in range(6)]
        bar = self.make(FakeDaemon(True, devices={"outputs": outs, "inputs": []}))
        self.open_settings(bar)
        row = bar.row("audio_source")
        self.assertTrue(row.cycle)
        self.assertEqual(row.cur.text(), "Default output")
        for _ in range(4):                       # record, resolution, fps, quality -> sound
            self.key(Qt.Key_Down)
        self.assertTrue(row.cur.hasFocus())
        self.key(Qt.Key_Right)
        self.assertEqual((row.value, row.cur.text()), ("out0.monitor", "Speaker 0"))
        self.key(Qt.Key_Left)
        self.key(Qt.Key_Left)                    # wraps to Off
        self.assertEqual(row.value, "off")
        self.shot(bar, "cycle", "settings")

    def test_settings_daemon_off_writes_config(self):
        tmp = Path(self._tmp.name) / "config.toml"
        tmp.write_text("# my settings\n[capture]\nresolution = \"1080p\"  \n")
        orig_path, orig_list = config.default_path, settings.list_audio_devices
        config.default_path = lambda: tmp
        settings.list_audio_devices = lambda *a, **k: DEVICES
        self.addCleanup(setattr, config, "default_path", orig_path)
        self.addCleanup(setattr, settings, "list_audio_devices", orig_list)
        daemon = FakeDaemon(False)
        bar = self.make(daemon)
        self.assert_off(bar)
        self.open_settings(bar)
        self.assertIn("off", bar.note.text())
        bar.row("resolution").buttons[0].click()   # 720p
        bar.row("audio_source").buttons[-1].click()  # Off
        self.shot(bar, "settings-offline", "settings")
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertIn("takes effect when Momento starts", bar.foot.text())
        text = tmp.read_text()
        self.assertIn("# my settings", text)
        cfg = config.load(tmp)
        self.assertEqual(cfg["capture"]["resolution"], "720p")
        self.assertFalse(cfg["audio"]["desktop"])
        self.wait_for(lambda: bar.mode == "clip", timeout=overlay.APPLY_CLOSE_MS / 1000 + 2)
        self.assert_off(bar)

    # ---------------------------------------------------------------- pause / stop / start

    def test_pause_and_resume(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        h0 = bar.height()
        self.assertEqual(bar.controls[0]["pause"].kind, "pause")
        self.key(Qt.Key_P)
        self.wait_for(lambda: bar.paused)
        pump(self.app, 0.1)
        self.assertEqual(daemon.controls, ["pause"])
        self.assertEqual(bar.name.text(), "Paused")
        self.assertEqual(bar.controls[0]["pause"].kind, "play")
        self.assertFalse(bar.hintbar.isHidden())
        self.assertIn("saving uses the footage so far", bar.hintbar.text())
        self.assertNotIn("fresh", bar.hintbar.text())
        self.assertTrue(all(o.isEnabled() for o in bar.options))
        self.assertEqual(bar.height(), h0 + overlay.HINT_H + 1)
        self.assertEqual(bar.stack.currentIndex(), 0)     # clips still saveable
        bar.controls[0]["pause"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "paused", "controls")
        self.shot(bar, "paused", "v3")
        self.shot(bar, "paused", "v4")
        self.key(Qt.Key_P)
        self.wait_for(lambda: not bar.paused and not bar.control_busy)
        self.assertEqual(daemon.controls, ["pause", "resume"])
        pump(self.app, 0.1)
        self.assertTrue(bar.hintbar.isHidden())

    def test_paused_save_still_works(self):
        daemon = FakeDaemon(True, paused=True)
        bar = self.make(daemon)
        self.assertTrue(bar.paused)
        self.key(Qt.Key_2)
        self.wait_done(bar)
        self.assertEqual(daemon.saves, [30])

    def test_stop_confirm(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        stop = bar.controls[0]["stop"]
        stop.setFocus()
        self.key(Qt.Key_Return)
        self.assertEqual(bar.mode, "confirm")
        self.assertTrue(bar.stop_no.hasFocus())   # safe default
        self.assertIn("Stop recording?", bar.confirm.text())
        self.assertIn("The replay history is cleared.", bar.confirm.text())
        self.assertEqual((bar.stop_yes.text(), bar.stop_no.text()), ("Stop", "Cancel"))
        self.shot(bar, "stop-confirm", "controls")
        self.key(Qt.Key_Return)                   # Cancel
        self.assertEqual(bar.mode, "clip")
        self.assertEqual(daemon.controls, [])
        self.assertTrue(stop.hasFocus())
        self.key(Qt.Key_Return)
        self.key(Qt.Key_Right)
        self.assertTrue(bar.stop_yes.hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "stop-confirm-focus", "controls")
        self.shot(bar, "stop-confirm", "v4")
        self.key(Qt.Key_Return)
        self.wait_for(lambda: daemon.controls == ["stop"] and not bar.control_busy)
        self.assertNotIn("quit", daemon.controls)
        self.assertTrue(daemon.running)           # the service (and the hotkey) stay up
        self.assert_off(bar)
        self.assertTrue(bar.running and bar.stopped)
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        bar.refresh_async()                       # the daemon itself now reports "stopped"
        self.wait_for(lambda: not bar.status_inflight)
        self.assertEqual(bar.last_status["state"], "stopped")
        self.assert_off(bar)
        self.assertEqual(bar.storage_hint.level, "ok")   # still reachable: free space is known
        self.shot(bar, "off-start", "controls")
        self.shot(bar, "stopped-off", "v4")
        # play resumes the running service instead of starting a new one
        orig = overlay.start_daemon
        overlay.start_daemon = lambda: self.fail("must not start a second daemon")
        self.addCleanup(setattr, overlay, "start_daemon", orig)
        self.key(Qt.Key_P)
        self.wait_for(lambda: daemon.controls == ["stop", "resume"] and not bar.control_busy)
        self.wait_for(lambda: bar.view == "rec")
        self.assertFalse(bar.stopped)

    def test_stop_older_daemon_falls_back_to_quit(self):
        daemon = FakeDaemon(True)
        orig_request = daemon.request

        def old(msg, **kw):
            if msg["cmd"] == "stop":
                return {"ok": False, "error": "unknown command 'stop'"}
            return orig_request(msg, **kw)
        daemon.request = old
        bar = self.make(daemon)
        bar.ask_stop()
        bar.confirm_stop()
        self.wait_for(lambda: daemon.controls == ["quit"] and not bar.control_busy)
        bar.refresh_async()
        self.wait_for(lambda: bar.view == "off" and not bar.running)
        self.assert_off(bar)

    def test_start_from_off(self):
        daemon = FakeDaemon(False)
        calls = []

        def fake_start():
            calls.append(1)
            daemon.extra = {"state": "starting", "recording": False, "buffered": 0}
            daemon.running = True
            return "process"
        orig = overlay.start_daemon
        overlay.start_daemon = fake_start
        self.addCleanup(setattr, overlay, "start_daemon", orig)
        bar = self.make(daemon)
        w0 = bar.size()
        play = bar.controls[0]["pause"]
        self.assertTrue(play.hasFocus())
        self.key(Qt.Key_Return)                   # play starts Momento
        self.assertEqual((bar.view, bar.name.text()), ("starting", "Starting"))
        self.assertEqual(bar.stack.currentIndex(), 0)
        self.assertEqual(bar.size(), w0)
        self.assertFalse(play.isEnabled())
        self.assertTrue(all(not o.isEnabled() for o in bar.options))
        pump(self.app, 0.05)
        self.shot(bar, "starting", "v3")
        pump(self.app, 0.8)                       # the daemon is up but not recording yet
        self.assertEqual(calls, [1])
        self.assertTrue(bar.control_busy)
        self.assertTrue(all(not o.isEnabled() for o in bar.options))
        daemon.extra = {}                         # now recording with footage
        self.wait_for(lambda: bar.online is True, timeout=5)
        self.assertEqual((bar.view, bar.stack.currentIndex()), ("rec", 0))
        self.assertTrue(all(o.isEnabled() for o in bar.options))
        want = overlay._last_choice()
        self.assertTrue(next(o for o in bar.options if o.seconds == want).hasFocus())
        self.assertEqual(bar.controls[0]["pause"].kind, "pause")
        self.assertTrue(bar.controls[0]["stop"].isEnabled())

    def test_play_key_starts_when_off(self):
        daemon = FakeDaemon(False)
        calls = []
        orig = overlay.start_daemon
        overlay.start_daemon = lambda: (calls.append(1), setattr(daemon, "running", True))
        self.addCleanup(setattr, overlay, "start_daemon", orig)
        bar = self.make(daemon)
        self.key(Qt.Key_P)
        self.wait_for(lambda: bar.online is True, timeout=5)
        self.assertEqual(calls, [1])

    def test_starting_after_restart_keeps_footage(self):
        # e.g. after Apply restarted the recorder: footage so far stays saveable
        bar = self.make(FakeDaemon(True, extra={"state": "starting", "recording": False}))
        self.assertEqual((bar.view, bar.name.text()), ("starting", "Starting"))
        self.assertTrue(all(o.isEnabled() for o in bar.options))
        bar2 = self.make(FakeDaemon(True, extra={"state": "starting", "recording": False, "buffered": 0}))
        self.assertTrue(all(not o.isEnabled() for o in bar2.options))
        self.assertEqual(bar2.stack.currentIndex(), 0)

    # ---------------------------------------------------------------- storage

    def test_low_storage_bar(self):
        daemon = FakeDaemon(True, extra={"state": "no_storage", "recording": False, "buffered": 0,
                                         "storage": LOW, "error": LOW_ERROR})
        bar = self.make(daemon)
        self.assertEqual(bar.stack.currentIndex(), 0)
        self.assertEqual((bar.view, bar.name.text()), ("lowstorage", "Low storage"))
        self.assertEqual(bar.time.text(), "")     # the numbers are in the strip above
        self.assertIn(overlay.RED, bar.dot.styleSheet())
        self.assertFalse(bar.hintbar.isHidden())
        self.assertIn("needs 7.2 GB, 3.1 GB free. Free up space", bar.hintbar.text())
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2 + overlay.HINT_H + 1)
        self.assertTrue(all(not o.isEnabled() for o in bar.options))
        self.assertTrue(bar.gear.hasFocus())      # the fix lives in settings
        self.key(Qt.Key_P)                         # play shows the warning instead of starting
        pump(self.app, 0.1)
        self.assertEqual(daemon.controls, [])
        self.assertFalse(bar.hintbar.isHidden())
        self.shot(bar, "lowstorage-bar", "v3")
        self.assertEqual(bar.storage_hint.level, "short")
        self.shot(bar, "lowstorage-red", "v4")
        # footage already buffered stays saveable
        daemon.extra["buffered"] = 120.0
        bar.apply_status(daemon.request({"cmd": "status"}))
        self.assertTrue(bar.options[0].isEnabled())
        self.assertEqual((bar.name.text(), bar.time.text()), ("Low storage", "2:00"))

    def test_paused_resume_blocked_by_storage(self):
        daemon = FakeDaemon(True, paused=True, extra={"storage": LOW})
        bar = self.make(daemon)
        self.assertEqual(bar.view, "paused")
        self.key(Qt.Key_P)
        pump(self.app, 0.1)
        self.assertEqual(daemon.controls, [])     # resume never sent
        self.assertIn("Not enough free space", bar.hintbar.text())

    def test_resume_reply_no_storage(self):
        err = "1440p Ultra needs 18.9 GB free, 9.4 GB available"
        # status without a storage block (an older daemon): the refusal's own warning must stay
        daemon = FakeDaemon(True, paused=True, storage=None,
                            resume_reply={"ok": False, "code": "no_storage", "error": err})
        bar = self.make(daemon)
        self.key(Qt.Key_P)
        self.wait_for(lambda: daemon.controls == ["resume"] and not bar.control_busy)
        pump(self.app, 0.05)
        self.assertEqual(bar.stack.currentIndex(), 0)
        self.assertIn("18.9 GB", bar.hintbar.text())
        self.assertFalse(bar.hintbar.isHidden())

    def test_settings_storage_blocks_apply(self):
        daemon = FakeDaemon(True, free=9.4e9)
        bar = self.make(daemon)
        self.open_settings(bar)
        res = bar.row("resolution")
        self.assertTrue(bar.apply_btn.isEnabled())
        self.assertIn("60 fps", bar.foot.text())
        self.assertEqual([b.property("nofit") for b in res.buttons], [False, False, True, True, True])
        self.key(Qt.Key_Down)                     # from Record to Resolution
        self.key(Qt.Key_Right)                    # 1440p: 10.8 GB > 9.4 GB
        self.assertEqual(res.value, "1440p")
        self.assertFalse(bar.apply_btn.isEnabled())
        self.assertIn("Needs 10.8 GB · 9.4 GB free", bar.foot.text())
        self.assertIn(overlay.RED, bar.foot.text())
        q = bar.row("quality")
        self.assertTrue(any(b.property("nofit") for b in q.buttons))
        self.key(Qt.Key_Return)                   # Enter does not apply
        pump(self.app, 0.1)
        self.assertEqual((bar.apply_state, daemon.configures), (None, []))
        for _ in range(5):
            self.key(Qt.Key_Down)
        self.assertTrue(bar.back_btn.hasFocus())  # Apply is skipped
        self.key(Qt.Key_Left)
        self.assertTrue(bar.back_btn.hasFocus())
        res.buttons[2].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "lowstorage-settings", "v3")
        self.key(Qt.Key_Left)                     # back to 1080p: fits again
        self.assertTrue(bar.apply_btn.isEnabled())
        self.assertNotIn("Needs", bar.foot.text())

    def test_settings_storage_lowering_always_allowed(self):
        # today's 1440p Ultra does not fit; a lower choice that still does not fit may be applied
        warn = "Not enough free space: needs 10.8 GB, 9.4 GB free"
        daemon = FakeDaemon(True, free=9.4e9, values={"resolution": "1440p", "quality": "ultra"},
                            configure_reply={"ok": True, "online": True, "state": "no_storage", "warning": warn})
        bar = self.make(daemon)
        self.open_settings(bar)
        self.assertIn("Needs", bar.foot.text())
        self.assertTrue(bar.apply_btn.isEnabled())         # no change is not a raise
        self.key(Qt.Key_Down)                               # from Record to Resolution
        self.key(Qt.Key_Right)                              # 4K: raises the requirement
        self.assertFalse(bar.apply_btn.isEnabled())
        self.key(Qt.Key_Left)
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Left)                               # quality: high, still > 9.4 GB
        self.assertEqual(bar.row("quality").value, "high")
        self.assertFalse(bar.fits(bar.pending()))
        self.assertTrue(bar.apply_btn.isEnabled())
        daemon.extra = {"state": "no_storage", "recording": False, "error": warn,
                        "storage": {**LOW, "free": 9.4e9, "required": 10.8e9}}
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"quality": "high"}])
        self.assertIn("needs 10.8 GB", bar.foot.text())
        self.assertNotIn("recording restarted", bar.foot.text())
        self.wait_for(lambda: bar.mode == "clip", timeout=overlay.APPLY_CLOSE_MS / 1000 + 2)
        self.assertEqual(bar.view, "lowstorage")
        self.assertIn("needs 10.8 GB", bar.hintbar.text())

    # ---------------------------------------------------------------- pills / focus / hints

    def test_mouse_click_leaves_no_focus_highlight(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        pause = bar.controls[0]["pause"]
        one = bar.options[2]
        self.assertEqual(one.visual_state, "focus")          # opened by the hotkey: focus shown
        self.assertEqual(pause.visual_state, "rest")
        self.app.sendEvent(pause, QEvent(QEvent.Enter))
        self.assertEqual(pause.visual_state, "hover")
        self.assertTrue(pause.active)
        QTest.mouseClick(pause, Qt.LeftButton)
        self.wait_for(lambda: bar.paused and not bar.control_busy)
        self.assertFalse(pause.hasFocus())                   # a click does not take focus
        self.assertFalse(bar.focus_visible)
        self.app.sendEvent(pause, QEvent(QEvent.Leave))
        self.assertEqual(pause.visual_state, "rest")         # the highlight goes with the mouse
        self.assertFalse(pause.active)
        pump(self.app, (overlay.ANIM_MS + 120) / 1000)
        fill, text, ring = pause.current()
        self.assertEqual(fill.name(), overlay.PILL_REST.lower())
        self.assertEqual(ring, 0.0)
        self.assertFalse(any(b.active for b in bar.pills()))  # nothing else lit either
        self.assertEqual(daemon.controls, ["pause"])
        # the keyboard brings focus back, on the widget that has it
        self.key(Qt.Key_Right)
        self.assertTrue(bar.focus_visible)
        self.assertEqual(bar.options[3].visual_state, "focus")
        self.assertEqual(one.visual_state, "rest")
        pump(self.app, (overlay.ANIM_MS + 120) / 1000)
        fill, text, ring = bar.options[3].current()
        self.assertEqual((fill.name(), text.name(), ring), (overlay.PILL_ON.lower(), "#111111", 1.0))

    def test_keyboard_focus_on_a_pill(self):
        bar = self.make(FakeDaemon(True))
        self.key(Qt.Key_Right)
        self.key(Qt.Key_Right)                               # 5m
        states = [o.visual_state for o in bar.options]
        self.assertEqual(states[4], "focus")
        self.assertEqual(states.count("focus"), 1)
        for o in bar.options:
            r = o.pill_rect()
            self.assertEqual(r.height(), overlay.PILL_H)
            self.assertAlmostEqual(r.center().y(), o.height() / 2)
        for b in bar.controls[0].values():                   # icon buttons are circles
            r = b.pill_rect()
            self.assertEqual((r.width(), r.height()), (overlay.PILL_H, overlay.PILL_H))
        self.shot(bar, "keyboard-focus-pill", "v4")
        self.key(Qt.Key_Tab)
        self.key(Qt.Key_Tab)
        self.key(Qt.Key_Tab)
        self.key(Qt.Key_Tab)                                 # 60m -> gear
        self.assertEqual(bar.gear.visual_state, "focus")
        self.assertEqual(bar.options[4].visual_state, "rest")

    def test_settings_selected_and_focused_pills(self):
        bar = self.make(FakeDaemon(True, free=9.4e9))
        self.open_settings(bar)
        res, fps, qual = bar.row("resolution"), bar.row("fps"), bar.row("quality")
        self.key(Qt.Key_Down)                                        # from Record to Resolution
        self.assertEqual(res.buttons[1].visual_state, "focus")      # 1080p: selected + focused
        self.assertEqual(qual.buttons[qual.idx].visual_state, "selected")
        self.assertEqual(fps.buttons[fps.idx].visual_state, "selected")
        self.assertEqual(res.buttons[0].visual_state, "rest")
        self.assertTrue(res.buttons[2].property("nofit"))            # still marked
        self.key(Qt.Key_Down)                                        # frame rate row
        self.assertEqual(res.buttons[1].visual_state, "selected")
        self.assertEqual(fps.buttons[fps.idx].visual_state, "focus")
        self.assertEqual(bar.apply_btn.pill_rect().height(), overlay.PILL_H)
        self.shot(bar, "settings-selected-focused", "v4")
        # a click on a value selects it and hides the keyboard ring
        QTest.mouseClick(qual.buttons[0], Qt.LeftButton)
        self.assertEqual(qual.value, "standard")
        self.assertFalse(bar.focus_visible)
        self.app.sendEvent(qual.buttons[0], QEvent(QEvent.Leave))
        self.assertEqual(qual.buttons[0].visual_state, "selected")
        self.assertEqual(fps.buttons[fps.idx].visual_state, "selected")

    def test_storage_hint_levels(self):
        cases = [(OK_STORAGE, "ok", overlay.GREEN, "742 GB"),
                 (TIGHT_STORAGE, "tight", overlay.YELLOW, "11.3 GB"),
                 ({**LOW, "ok": False}, "short", overlay.RED, "3.1 GB")]
        widths = set()
        for sto, level, color, label in cases:
            bar = self.make(FakeDaemon(True, storage=sto))
            self.assertFalse(bar.storage_hint.isHidden())
            self.assertEqual((bar.storage_hint.level, bar.storage_hint.color, bar.storage_hint.label),
                             (level, color, label))
            widths.add(bar.width())
            xs = tuple(o.mapTo(bar, o.rect().topLeft()).x() for o in bar.options)
            widths.add(xs)
            if level == "tight":
                self.shot(bar, "storage-yellow", "v4")
            if level == "short":
                self.shot(bar, "storage-red", "v4")
        self.assertEqual(len(widths), 2)                     # same width, same pill positions
        self.assertEqual(overlay._storage_level({"free": 9e9, "reclaimable": 0, "required": 5e9}), "tight")
        self.assertEqual(overlay._storage_level({"free": 3e9, "reclaimable": 2.5e9, "required": 5e9}), "tight")
        self.assertEqual(overlay._storage_level({"free": 3e9, "reclaimable": 0, "required": 5e9}), "short")
        self.assertEqual(overlay._free_label(9.4e9), "9.4 GB")
        self.assertEqual(overlay._free_label(1.25e12), "1.2 TB")

    def test_storage_hint_hidden_without_info(self):
        bar = self.make(FakeDaemon(True, storage=None))       # an older daemon
        self.assertTrue(bar.storage_hint.isHidden())
        self.assertIsNone(bar.storage_hint.level)
        off = self.make(FakeDaemon(False))                   # daemon not running
        self.assertTrue(off.storage_hint.isHidden())
        self.assertEqual(bar.width(), off.width())

    # ---------------------------------------------------------------- timings / result states

    def test_result_timings(self):
        self.assertLessEqual(overlay.RESULT_CLOSE_MS, 1200)
        self.assertLessEqual(overlay.APPLY_CLOSE_MS, 1200)
        self.assertLessEqual(overlay.STOP_CLOSE_MS, 800)
        self.assertEqual(overlay.IDLE_CLOSE_MS, 10_000)

    def test_stop_hides_quickly_and_cancel_restarts_idle(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.resident = True                       # hiding is observable (a one-shot bar would quit)
        self.assertEqual((bar.stop_yes.glyph, bar.stop_no.glyph), ("stopsq", "cross"))
        bar.ask_stop()
        bar.idle.stop()
        bar.cancel_confirm()                      # back to the clip view, normal auto-hide again
        self.assertEqual(bar.mode, "clip")
        self.assertTrue(bar.idle.isActive())
        self.assertEqual(bar.idle.interval(), overlay.IDLE_CLOSE_MS)
        bar.ask_stop()
        pump(self.app, 0.05)
        self.shot(bar, "stop-confirm-icons", "v5")
        t0 = time.monotonic()
        bar.confirm_stop()
        self.wait_for(lambda: daemon.controls == ["stop"] and not bar.control_busy)
        self.assertEqual(bar.view, "off")         # the Off state shows briefly...
        self.assertTrue(bar.isVisible())
        self.wait_for(lambda: not bar.isVisible(), timeout=overlay.STOP_CLOSE_MS / 1000 + 1)
        self.assertLess(time.monotonic() - t0, overlay.STOP_CLOSE_MS / 1000 + 0.8)  # ...then it hides

    def test_apply_hides_after_result(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.resident = True
        self.open_settings(bar)
        bar.row("quality").buttons[0].click()
        bar.apply_settings()
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertTrue(bar.isVisible())
        self.wait_for(lambda: not bar.isVisible(), timeout=overlay.APPLY_CLOSE_MS / 1000 + 1)
        self.assertEqual(bar.mode, "clip")

    # ---------------------------------------------------------------- the recording timer

    def test_timer_shows_live_time_but_decides_on_saveable(self):
        daemon = FakeDaemon(True, extra={"buffered": 10.0, "buffered_live": 13.5})
        bar = self.make(daemon)
        self.assertEqual(bar.time.text(), "0:13")               # the display: buffered_live
        self.assertEqual(bar.buffered, 10.0)                    # the logic: buffered
        self.assertTrue(bar.options[0].property("long"))        # 15s > 10 s saveable
        self.assertTrue(bar.live_ticking and bar.ticker.isActive())
        self.wait_for(lambda: bar.time.text() == "0:14", timeout=1.5)  # ticks between polls
        bar.choose(bar.options[0])
        self.assertIn("Saving last 10s", bar.line.text())       # clamped to what is saveable
        self.wait_done(bar)
        # an older daemon without buffered_live: the time is buffered
        bar2 = self.make(FakeDaemon(True))
        self.assertEqual(bar2.time.text(), "12:34")
        # a poll slightly behind the local tick never steps the time back
        bar2.set_live({"buffered": 754.0, "buffered_live": 800.0, "max_seconds": 3600}, True)
        bar2.live = (800.0, time.monotonic() - 0.9)
        bar2.set_live({"buffered": 754.0, "buffered_live": 800.2, "max_seconds": 3600}, True)
        self.assertGreaterEqual(bar2.live_seconds(), 800.9)
        # never past the buffer length
        bar2.set_live({"buffered_live": 3599.5, "max_seconds": 3600}, True)
        bar2.live = (3599.5, time.monotonic() - 5)
        self.assertEqual(bar2.live_seconds(), 3600)
        paused = self.make(FakeDaemon(True, paused=True, extra={"buffered_live": 13.5, "buffered": 10.0}))
        self.assertFalse(paused.live_ticking or paused.ticker.isActive())

    # ---------------------------------------------------------------- window mode

    def test_record_row(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.open_settings(bar)
        row = bar.row("record")
        self.assertIs(bar.rows[0], row)
        self.assertEqual([b.text() for b in row.buttons], ["Full screen", "Game window"])
        self.assertEqual((row.value, row.icon.kind), ("screen", "fullscreen"))
        self.assertTrue(bar.change_btn.isHidden())                   # full screen: nothing to change
        xs = {r.icon.mapTo(bar, r.icon.rect().topLeft()).x() for r in bar.visible_rows()}
        self.assertEqual(len(xs), 1)
        self.assertLessEqual(row.sizeHint().width(), bar.width())
        self.assertTrue(row.buttons[0].hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "settings-record", "v5")
        self.key(Qt.Key_Right)                                       # Game window
        self.assertEqual((row.value, row.icon.kind), ("window", "window"))
        self.assertTrue(bar.change_btn.isHidden())                   # not applied yet
        self.assertEqual(bar.changes(), {"record": "window"})
        pump(self.app, 0.05)
        self.shot(bar, "settings-record-window", "v5")
        bar.resident = True
        self.key(Qt.Key_Return)
        self.wait_for(lambda: daemon.configures == [{"record": "window"}])
        # the window picker opens now: the bar gets out of its way right after the reply
        self.wait_for(lambda: not bar.isVisible(), timeout=2)

    def test_change_window_pill(self):
        daemon = FakeDaemon(True, values={"record": "window"}, extra={"target": "window"})
        bar = self.make(daemon)
        w0 = bar.width()
        self.open_settings(bar)
        row = bar.row("record")
        self.assertEqual((row.value, row.icon.kind), ("window", "window"))
        self.assertFalse(bar.change_btn.isHidden())
        self.assertEqual(bar.change_btn.text(), "Change window")
        self.assertTrue(row.isAncestorOf(bar.change_btn))
        self.assertEqual(bar.width(), w0)
        self.assertTrue(row.buttons[1].hasFocus())
        self.key(Qt.Key_Right)                                       # past "Game window": the pill
        self.assertTrue(bar.change_btn.hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "settings-change-window", "v5")
        self.key(Qt.Key_Left)
        self.assertTrue(row.buttons[1].hasFocus())
        self.key(Qt.Key_Left)                                        # Full screen hides the pill
        self.assertTrue(bar.change_btn.isHidden())
        self.key(Qt.Key_Right)
        self.assertFalse(bar.change_btn.isHidden())
        self.key(Qt.Key_Right)
        bar.resident = True
        self.key(Qt.Key_Return)                                      # Change window
        self.wait_for(lambda: daemon.controls == ["pick_window"])
        self.wait_for(lambda: not bar.isVisible(), timeout=2)       # out of the picker's way
        self.assertEqual(daemon.configures, [])                      # nothing applied

    def test_change_window_hidden_when_off(self):
        tmp = Path(self._tmp.name) / "config-window.toml"
        tmp.write_text('[capture]\ntarget = "window"\n')
        orig_path, orig_list = config.default_path, settings.list_audio_devices
        config.default_path = lambda: tmp
        settings.list_audio_devices = lambda *a, **k: DEVICES
        self.addCleanup(setattr, config, "default_path", orig_path)
        self.addCleanup(setattr, settings, "list_audio_devices", orig_list)
        bar = self.make(FakeDaemon(False))
        self.open_settings(bar)
        self.assertEqual(bar.row("record").value, "window")
        self.assertTrue(bar.change_btn.isHidden())                   # Momento is off: nothing to change

    NO_WINDOW = {"state": "no_window", "recording": False, "target": "window",
                 "error": "The game window closed \u2014 pick a window to keep recording"}

    def test_no_window_bar(self):
        ref = self.make(FakeDaemon(True))                            # a recording bar, for its width
        rec_w = ref.width()
        ref.close()
        self.app.removeEventFilter(ref)
        daemon = FakeDaemon(True, extra=dict(self.NO_WINDOW))
        bar = self.make(daemon)
        self.assertEqual(bar.stack.currentIndex(), 0)
        self.assertEqual((bar.view, bar.name.text(), bar.time.text()), ("nowindow", "No window", "12:34"))
        self.assertIn(overlay.MUTED, bar.dot.styleSheet())
        self.assertFalse(bar.hintbar.isHidden())
        self.assertEqual(bar.hintbar.text(), overlay.NO_WINDOW_HINT)
        self.assertEqual(overlay.NO_WINDOW_HINT, "Game closed · press play to pick a window")
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2 + overlay.HINT_H + 1)
        self.assertEqual(bar.width(), rec_w)                         # same bar, same width
        self.assertTrue(all(o.isEnabled() for o in bar.options))     # the footage is saveable
        play = bar.controls[0]["pause"]
        self.assertEqual((play.kind, play.accessibleName()), ("pick", "Pick a game window"))
        self.assertTrue(play.isEnabled())
        self.assertFalse(bar.live_ticking)
        self.shot(bar, "no-window", "v5")
        play.setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "no-window-play-focus", "v5")
        bar.resident = True
        self.key(Qt.Key_P)                                           # play = pick a window
        self.wait_for(lambda: daemon.controls == ["pick_window"])
        self.wait_for(lambda: not bar.isVisible(), timeout=2)       # the picker dialog gets the screen
        self.assertNotIn("resume", daemon.controls)

    def test_no_window_without_footage(self):
        daemon = FakeDaemon(True, extra={**self.NO_WINDOW, "buffered": 0})
        bar = self.make(daemon)
        self.assertEqual(bar.view, "nowindow")
        self.assertTrue(all(not o.isEnabled() for o in bar.options))
        self.assertTrue(bar.controls[0]["pause"].hasFocus())         # play is the next step
        QTest.mouseClick(bar.controls[0]["pause"], Qt.LeftButton)
        self.wait_for(lambda: daemon.controls == ["pick_window"])

    def test_kwin_version_gate(self):
        parse = overlay._parse_kwin_version
        self.assertEqual(parse("Version\n=======\nKWin version: 6.6.0\nQt Version: 6.10.1\n"), (6, 6, 0))
        self.assertEqual(parse("kwin 6.7.4\n"), (6, 7, 4))
        self.assertEqual(parse("kwin_wayland 6.5.91"), (6, 5, 91))
        self.assertEqual(parse("KWin version: 6.6"), (6, 6, 0))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("Qt Version: 6.10.1"))
        can = overlay._kwin_can_exclude
        self.assertTrue(can((6, 6, 0)) and can((6, 7, 4)) and can((7, 0, 0)))
        self.assertFalse(can((6, 5, 91)) or can((5, 27, 11)) or can(None))


class ResidentBar(unittest.TestCase):
    """The resident bar: built once, hidden, driven over its control socket."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        cls._tmp = tempfile.TemporaryDirectory()
        cls._last = overlay.LAST_FILE
        overlay.LAST_FILE = Path(cls._tmp.name) / "overlay.last"
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        ipc.request = REAL_REQUEST
        overlay.LAST_FILE = cls._last
        cls._tmp.cleanup()

    wait_for = OverlayOffscreen.wait_for
    key = OverlayOffscreen.key

    def make(self, daemon):
        real = REAL_REQUEST

        def route(msg, timeout=120, path=None, **kw):
            if path is not None:           # the bar's control socket: the real thing
                return real(msg, timeout=timeout, path=path)
            return daemon.request(msg, timeout=timeout)
        ipc.request = route
        type(self).n += 1
        # In the sandboxed runtime dir (tests/_sandbox.py), never the user's.
        self.sock = config.RUNTIME_DIR / f"overlay-test-{self.n}.sock"
        self.assertTrue(str(self.sock).startswith(os.environ["MOMENTO_TEST_SANDBOX"]))
        bar, server = overlay.start_resident(self.app, False, path=self.sock)
        self.addCleanup(bar.dismiss)
        self.addCleanup(self.app.removeEventFilter, bar)
        self.addCleanup(server.close)
        pump(self.app, 0.05)
        return bar

    def send(self, cmd, timeout=3):
        box = []
        t = threading.Thread(target=lambda: box.append(overlay.send_resident(cmd, timeout=timeout,
                                                                            path=self.sock)))
        t.start()
        while t.is_alive():
            pump(self.app, 0.005)
        pump(self.app, 0.03)                                  # window activation lands
        return box[0]

    def assert_fresh(self, bar):
        """What a bar that was just launched looks like."""
        self.assertTrue(bar.isVisible())
        self.assertEqual((bar.mode, bar.stack.currentIndex()), ("clip", 0))
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)
        self.assertTrue(bar.panel.isHidden())
        self.assertEqual(bar.rows, [])
        self.assertFalse(bar.saving or bar.done or bar.control_busy or bar.loading_settings)
        self.assertIsNone(bar.apply_state)
        self.assertEqual(bar.line.text(), "")
        self.assertTrue(bar.focus_visible)
        self.assertEqual(bar.idle.interval(), overlay.IDLE_CLOSE_MS)
        self.assertTrue(bar.idle.isActive() and bar.poll.isActive())
        want = overlay._last_choice()
        self.assertTrue(next(o for o in bar.options if o.seconds == want).hasFocus())

    def test_toggle_show_hide(self):
        bar = self.make(FakeDaemon(True))
        self.assertTrue(bar.resident)
        self.assertFalse(bar.isVisible())                     # built, not mapped
        self.assertEqual(oct(os.stat(self.sock).st_mode & 0o777), "0o600")
        self.assertEqual(bar.time.text(), "12:34")            # status cached at startup
        r = self.send("toggle")
        self.assertEqual((r["ok"], r["visible"]), (True, True))
        self.assert_fresh(bar)
        self.assertEqual(self.send("toggle")["visible"], False)
        self.assertFalse(bar.isVisible())
        self.assertTrue(self.send("show")["visible"])
        self.assertTrue(self.send("show")["visible"])         # show on a shown bar keeps it
        self.assertTrue(bar.isVisible())
        self.assertFalse(self.send("hide")["visible"])
        self.assertFalse(self.send("hide")["visible"])
        self.assertEqual(self.send("ping")["visible"], False)
        self.assertFalse(self.send("bogus")["ok"])

    def test_show_latency(self):
        bar = self.make(FakeDaemon(True))
        painted = []

        def spy(obj, ev, _orig=bar.eventFilter):
            if obj is bar and ev.type() == QEvent.Paint and not painted:
                painted.append(time.perf_counter())
            return _orig(obj, ev)
        bar.eventFilter = spy
        t0 = time.perf_counter()
        self.send("show")
        self.wait_for(lambda: painted)
        took = painted[0] - t0
        sys.stderr.write(f"\n  resident bar: show -> first paint {took * 1000:.1f} ms\n")
        self.assertLess(took, 0.25)

    def test_state_reset_between_opens(self):
        overlay.LAST_FILE.unlink(missing_ok=True)             # default length: 1m
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.send("show")
        self.key(Qt.Key_S)                                    # settings open...
        self.wait_for(lambda: bar.mode == "settings")
        self.assertGreater(bar.height(), overlay.BAR_HEIGHT + 2)
        self.send("hide")                                     # ...and the bar is hidden
        self.assertEqual(bar.rows, [])                        # settings rows are let go
        self.send("show")
        self.assert_fresh(bar)                                # back on the clip lengths
        bar.ask_stop()                                        # the stop question
        self.assertEqual(bar.mode, "confirm")
        self.send("toggle")
        self.send("toggle")
        self.assert_fresh(bar)
        QTest.mouseClick(bar.gear, Qt.LeftButton)             # the mouse hides the focus ring
        self.assertFalse(bar.focus_visible)
        self.wait_for(lambda: bar.mode == "settings")
        self.send("toggle")
        self.send("toggle")
        self.assert_fresh(bar)                                # opened by the hotkey: ring shown
        self.key(Qt.Key_Right)
        self.key(Qt.Key_Return)                               # save 3m
        self.wait_for(lambda: bar.done)
        self.assertIn("Saved", bar.line.text())
        # "Saved" hides the bar (a one-shot bar quits here); the process stays
        self.wait_for(lambda: not bar.isVisible(), timeout=overlay.RESULT_CLOSE_MS / 1000 + 2)
        self.assertTrue(self.send("ping")["ok"])
        self.assertEqual(overlay._last_choice(), 180)
        self.send("show")
        self.assert_fresh(bar)                                # no stale "Saved"
        self.assertTrue(bar.options[3].hasFocus())            # the last choice

    def test_stale_reply_is_dropped(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.send("show")
        self.key(Qt.Key_1)                                    # save in flight (0.2 s)
        self.assertTrue(bar.saving)
        self.send("hide")
        self.send("show")
        pump(self.app, 0.5)
        self.assertEqual(daemon.saves, [15])
        self.assert_fresh(bar)                                # the old "Saved" never lands

    def test_esc_and_idle_hide(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.send("show")
        self.key(Qt.Key_Escape)
        self.assertFalse(bar.isVisible())
        self.assertTrue(self.send("ping")["ok"])              # hidden, not quit
        self.send("show")
        bar.on_idle()                                         # the idle timeout
        self.assertFalse(bar.isVisible())
        self.assertFalse(bar.idle.isActive() or bar.poll.isActive())

    def test_hidden_bar_holds_no_focus(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.send("show")
        self.assertIs(self.app.activeWindow(), bar)
        self.assertTrue(bar.isAncestorOf(self.app.focusWidget()))
        self.send("hide")
        pump(self.app, 0.05)
        self.assertIsNone(self.app.activeWindow())
        self.assertIsNone(self.app.focusWidget())
        # keys that still reach it (they should not) do nothing
        for k in (Qt.Key_P, Qt.Key_1, Qt.Key_S):
            self.app.sendEvent(bar, QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))
        pump(self.app, 0.2)
        self.assertEqual((daemon.controls, daemon.saves, bar.mode), ([], [], "clip"))
        self.assertFalse(bar.idle.isActive() or bar.poll.isActive())

    def test_cached_status_first_then_fresh(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.status_at -= 5                                    # hidden for 5 s while recording
        daemon.extra = {"buffered": 900.0}
        bar.present()                                         # what "show" runs, before any reply
        self.assertTrue(bar.isVisible())
        self.assertEqual(bar.time.text(), "12:39")            # cached, grown by 5 s: no IPC wait
        self.wait_for(lambda: bar.time.text() == "15:00")     # then the daemon's own number

    def test_repeated_open_does_not_leak(self):
        bar = self.make(FakeDaemon(True))

        def cycle():
            self.send("show")
            self.key(Qt.Key_S)
            self.wait_for(lambda: bar.mode == "settings")
            self.send("hide")
            pump(self.app, 0.05)

        cycle()
        n = len(bar.findChildren(QWidget))
        for _ in range(10):
            cycle()
        sys.stderr.write(f"\n  widgets after 1 open: {n}, after 11: {len(bar.findChildren(QWidget))}\n")
        self.assertEqual(len(bar.findChildren(QWidget)), n)

    def test_second_resident_refused(self):
        bar = self.make(FakeDaemon(True))
        with self.assertRaises(RuntimeError):
            overlay.ControlServer(self.sock, lambda m: {"ok": True}).start()
        self.assertTrue(self.send("ping")["ok"])              # the first one keeps its socket
        self.assertFalse(bar.isVisible())

    def test_fallback_without_resident(self):
        from momento import cli

        missing = config.RUNTIME_DIR / "no-such-overlay.sock"
        orig_sock, orig_main, orig_pid = overlay.CONTROL_SOCKET, overlay.main, overlay.PIDFILE
        self.addCleanup(setattr, overlay, "CONTROL_SOCKET", orig_sock)
        self.addCleanup(setattr, overlay, "main", orig_main)
        self.addCleanup(setattr, overlay, "PIDFILE", orig_pid)
        overlay.PIDFILE = config.RUNTIME_DIR / "no-such-overlay.pid"
        ipc.request = REAL_REQUEST
        overlay.CONTROL_SOCKET = missing
        self.assertIsNone(overlay.send_resident("toggle"))
        self.assertFalse(overlay.toggle())
        launched = []
        overlay.main = lambda argv=None: launched.append(list(argv or [])) or 0
        self.assertEqual(cli.main(["overlay"]), 0)
        self.assertEqual(launched, [[]])                      # the one-shot bar, as before
        self.assertEqual(cli.main(["overlay", "--resident"]), 0)
        self.assertEqual(launched, [[], ["--resident"]])
        # with a resident bar listening, `momento overlay` toggles it instead
        bar = self.make(FakeDaemon(True))
        overlay.CONTROL_SOCKET = self.sock
        box = []
        t = threading.Thread(target=lambda: box.append(cli.main(["overlay"])))
        t.start()
        while t.is_alive():
            pump(self.app, 0.005)
        self.assertEqual(box, [0])
        self.assertEqual(launched, [[], ["--resident"]])
        self.assertTrue(bar.isVisible())


if __name__ == "__main__":
    unittest.main()

