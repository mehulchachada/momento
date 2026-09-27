"""Render the replay bar offscreen with a stubbed daemon and save screenshots.

    python3 -m unittest tests.test_overlay_offscreen

Screenshots land in /tmp/claude-1000/momento-{overlay,settings,controls,screenshot,v3,...,v7}-*.png
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

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPixmap, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from momento import config, gamepad, ipc, overlay, quality, settings  # noqa: E402

SHOT_DIR = Path(os.environ.get("MOMENTO_SHOT_DIR", "/tmp/claude-1000"))
REAL_REQUEST = ipc.request  # the resident bar's control socket is always reached for real
# The offscreen screen is 800x600, which would cap every resolution: no screen size
# unless a test sets one (the Resolution cap tests do).
overlay.SCREEN_SIZE = lambda: None

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


# What a settings reply carries for the keys added with the tabs (used when the
# settings module in the tree predates them).
NEW_VALUES = {"keep_history": "off", "hour_warning": 10, "instant_bar": "on", "replay_length": 60}
NEW_CHOICES = {"keep_history": ["off", "on"], "hour_warning": [10, 5, 3], "instant_bar": ["on", "off"],
               "replay_length": [15, 30, 60]}


def settings_reply(devices=DEVICES, free=None, source=None, **values):
    cfg = config.load(Path("/nonexistent/momento-test.toml"))
    cfg["buffer"]["max_seconds"] = 3600   # the fake daemon keeps 60 minutes (STATUS) unless told otherwise
    raw = {}
    for k, v in values.items():
        try:
            for section, key, val in settings.writes(k, settings.normalize(k, v)):
                cfg.setdefault(section, {})[key] = val
        except ValueError:
            raw[k] = v                   # a key this settings module does not know yet
    data = settings.describe(cfg, devices=devices, source=source)
    for k, v in NEW_VALUES.items():
        data["values"].setdefault(k, v)
        data["choices"].setdefault(k, list(NEW_CHOICES[k]))
    data["values"].update(raw)
    data.setdefault("tabs", [[n, list(k)] for n, k in overlay.DEFAULT_TABS])
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
# A daemon with the low-storage rule: a full 60 min at 1080p High doesn't fit (still recording).
SHORT_SPAN = {"ok": False, "free": 5_100_000_000, "reclaimable": 0, "required": 8_200_000_000,
              "path": "/home/user/.cache/momento", "low": True, "needed": 8_200_000_000,
              "available": 5_100_000_000, "history": False, "disk": "buffer", "label": "1080p High"}
SHORT_SPAN_MSG = "Low storage: 60 min at 1080p High needs 8.2 GB, 5.1 GB free. Free up space."
# Keep history: the buffer fits, the hour it saves to Videos doesn't (yellow).
SHORT_HISTORY = {**SHORT_SPAN, "ok": True, "free": 11_300_000_000, "needed": 15_400_000_000,
                 "available": 11_300_000_000, "history": True}


class FakeDaemon:
    def __init__(self, running=True, fail=False, devices=DEVICES, paused=False, extra=None, free=None,
                 resume_reply=None, values=None, configure_reply=None, storage=OK_STORAGE, source=None):
        self.running, self.fail, self.devices, self.paused = running, fail, devices, paused
        self.extra = dict(extra or {})   # merged into every status reply
        self.free = free                 # settings: free bytes for the storage check
        self.resume_reply = resume_reply
        self.values = dict(values or {})  # settings: current config values
        self.configure_reply = configure_reply
        self.storage = storage           # status: the storage block (None = an older daemon)
        self.source = source             # settings: the recorded picture's size (None: not known yet)
        self.stopped = False
        self.saves = []
        self.save_msgs = []
        self.configures = []
        self.controls = []
        self.gallery_controls = []       # pause / resume with reason "gallery" (like the daemon's)
        self.pause_reason = None
        self.gallery_pid = None
        self.shots = []                  # (monotonic time, what on_shot() saw) per screenshot request
        self.on_shot = None              # called when a screenshot request arrives (e.g. is the bar visible?)

    def request(self, msg, timeout=120, **_):
        if not self.running:
            raise ipc.DaemonNotRunning("no socket")
        if msg["cmd"] == "status":
            st = dict(STATUS)
            if self.storage:
                st["storage"] = dict(self.storage)
            if self.stopped:
                st.update(state="stopped", recording=False, stop_reason="user")
                if not self.extra.get("keep_history"):
                    st["buffered"] = 0
            elif self.paused:
                st.update(state="paused", recording=False, pause_reason=self.pause_reason)
            st.update(self.extra)
            return st
        if msg["cmd"] == "settings":
            return settings_reply(self.devices, free=self.free, source=self.source, **self.values)
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
            # the service keeps running; recording stops, the history is cleared unless kept
            self.controls.append("stop")
            self.stopped, self.paused = True, False
            kept = bool(self.extra.get("keep_history"))
            if not kept:
                self.extra.pop("buffered", None)
            return {"ok": True, "state": "stopped", "buffer_cleared": not kept}
        if msg["cmd"] in ("pause", "resume") and msg.get("reason") == "gallery":
            self.gallery_controls.append(msg["cmd"])
            st = self.request({"cmd": "status"})
            if msg["cmd"] == "pause":
                if st["state"] == "recording" and st.get("target", "screen") == "screen":
                    self.paused, self.pause_reason, self.gallery_pid = True, "gallery", msg.get("pid")
                return {"ok": True, "state": "paused" if self.paused else st["state"],
                        "pause_reason": self.pause_reason}
            if self.pause_reason == "gallery":
                self.paused, self.pause_reason = False, None
                return {"ok": True, "state": "starting"}
            return {"ok": True, "state": st["state"]}
        if msg["cmd"] in ("pause", "resume", "quit"):
            self.controls.append(msg["cmd"])
            self.pause_reason = None     # the user's pause / play takes a gallery pause over
            if msg["cmd"] == "resume" and self.resume_reply:
                return self.resume_reply
            if msg["cmd"] == "pause":
                self.paused = True
            elif msg["cmd"] == "resume":
                self.paused = self.stopped = False
            elif msg["cmd"] == "quit":
                self.running = False
            return {"ok": True, "state": "paused" if self.paused else "starting"}
        if msg["cmd"] == "screenshot":
            self.shots.append((time.monotonic(), self.on_shot() if self.on_shot else None))
            return {"ok": True, "path": "/home/user/Videos/Momento/Images/Momento_2026-09-26_21-04-11.png",
                    "width": 1920, "height": 1080}
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
        # 996 px before the gallery button (+40 for it, +4 gap): still a slim bar up to 1040
        self.assertLess(bar.width(), 1050)
        for o in bar.options:
            self.assertGreaterEqual(o.width(), overlay.OPTION_MIN_WIDTH)

    def test_save_sends_plain_request(self):
        # The clip runs up to now: no "until", whatever the desktop (the bar may be in a
        # full-screen clip; recording a window keeps it out).
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.choose(bar.options[1])
        self.wait_for(lambda: daemon.save_msgs)
        self.assertEqual(daemon.save_msgs[0], {"cmd": "save", "seconds": 30})
        for gone in ("CAPTURE_EXCLUDED", "_exclude_from_capture", "_parse_kwin_version", "_kwin_version"):
            self.assertFalse(hasattr(overlay, gone), gone)   # no compositor-specific code in the bar

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
        # recording: the logo, the red dot, what is recorded and the time
        self.assertEqual((bar.view, bar.name.text()), ("rec", "Recording Full Screen"))
        self.assertFalse(bar.name.isHidden())
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

    def assert_stopped(self, bar, target="screen"):
        """Recording stopped, the service still up: one sentence says what play does."""
        self.assertEqual(bar.stack.currentIndex(), 0)
        self.assertEqual(bar.view, "stopped")
        self.assertEqual(bar.name.text(), overlay.STOPPED_TEXT[target])
        self.assertEqual(bar.time.text(), "")
        self.assertTrue(bar.dotbox.isHidden())
        play = bar.controls[0]["pause"]
        self.assertEqual(play.kind, "pick" if target == "window" else "start")
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
        self.key(Qt.Key_Left)  # the disabled lengths are skipped: the gallery button
        self.assertTrue(bar.gallery_btn.hasFocus())
        self.key(Qt.Key_Left)  # wraps to the gear
        self.assertTrue(bar.gear.hasFocus())
        self.key(Qt.Key_Right)
        self.assertTrue(bar.gallery_btn.hasFocus())
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

    def test_lengths_past_the_replay_are_disabled(self):
        """A 15-minute replay: 30m and 60m are greyed like any disabled pill and never saved."""
        daemon = FakeDaemon(True, extra={"max_seconds": 900})
        self.addCleanup(overlay._store_choice, overlay._last_choice())
        overlay._store_choice(1800)                               # last time: 30m
        bar = self.make(daemon)
        self.assertEqual([o.text() for o in bar.options if not o.isEnabled()], ["30m", "60m"])
        self.assertEqual(bar.options[6].visual_state, "disabled")
        self.assertTrue(bar.options[5].hasFocus())                # 15m: the longest the replay holds
        self.key(Qt.Key_7)                                        # 30m by its number: nothing
        QTest.mouseClick(bar.options[7], Qt.LeftButton)           # 60m by a click: nothing
        pump(self.app, 0.1)
        self.assertEqual((daemon.saves, bar.saving), ([], False))
        bar.options[5].setFocus()
        self.key(Qt.Key_Right)                                    # past 15m: straight to the buttons
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.shot(bar, "replay-15m", "v8")
        daemon.extra["max_seconds"] = 1800                        # a longer replay: 30m comes back
        bar.apply_status(daemon.request({"cmd": "status"}))
        self.assertEqual([o.text() for o in bar.options if not o.isEnabled()], ["60m"])
        daemon.extra["max_seconds"] = 3600
        bar.apply_status(daemon.request({"cmd": "status"}))
        self.assertTrue(all(o.isEnabled() for o in bar.options))

    def test_gear_reachable_past_60m(self):
        bar = self.make(FakeDaemon(True))
        bar.options[-1].setFocus()
        self.key(Qt.Key_Right)
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.key(Qt.Key_Tab)
        self.assertTrue(bar.controls[0]["stop"].hasFocus())
        self.key(Qt.Key_Tab)
        self.assertTrue(bar.controls[0]["shot"].hasFocus())
        self.key(Qt.Key_Tab)
        self.assertTrue(bar.gear.hasFocus())
        self.key(Qt.Key_Tab)  # wraps to the gallery button, left of the lengths
        self.assertTrue(bar.gallery_btn.hasFocus())
        self.key(Qt.Key_Tab)
        self.assertTrue(bar.options[0].hasFocus())
        self.key(Qt.Key_Left)
        self.assertTrue(bar.gallery_btn.hasFocus())
        self.key(Qt.Key_Left)
        self.assertTrue(bar.gear.hasFocus())
        self.shot(bar, "clip-gear-focus", "settings")
        bar.options[2].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "clip", "settings")

    PANEL = overlay.PANEL_PAD_T + overlay.TABS_H + 4 * overlay.ROW_PITCH + overlay.PANEL_PAD_B

    def test_settings_keyboard_and_apply(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.move(100, 700)
        h0, bottom0 = bar.height(), bar.y() + bar.height()
        bar.gear.setFocus()
        self.key(Qt.Key_Return)  # activates the gear
        self.wait_for(lambda: bar.mode == "settings")
        self.assertEqual(bar.height(), h0 + self.PANEL + 1)   # tabs + the tallest tab's 4 rows (Video)
        self.assertEqual(bar.y() + bar.height(), bottom0)  # grew upward
        self.assertEqual(bar.stack.currentIndex(), 2)
        self.assertIn("60 fps", bar.foot.text())
        self.assertIn("6.8 GB for 60 min", bar.foot.text())
        self.assertTrue(bar.row("mic_device").isHidden())
        self.assertEqual({r.key: r.icon.kind for r in bar.rows},
                         {"record": "window", "replay_length": "timer", "keep_history": "history",
                          "resolution": "display",
                          "fps": "gauge", "quality": "sliders", "format": "film", "audio_source": "speaker", "mic": "mic",
                          "mic_device": "micdev", "controller": "gamepad", "hour_warning": "hourglass",
                          "instant_bar": "bolt"})
        self.assertTrue(all(r.height() == overlay.ROW_PITCH for r in bar.rows))
        self.assertEqual((bar.apply_btn.glyph, bar.back_btn.glyph), ("check", "back"))
        self.assertEqual(bar.tab_names[bar.tab], "General")
        self.assertEqual(bar.rows[0].key, "record")                 # what to record comes first
        self.assertTrue(bar.row("record").buttons[1].hasFocus())    # Window (the default)
        self.key(Qt.Key_Up)                                         # up to the tab row
        self.assertTrue(bar.tab_btns[0].hasFocus())
        self.key(Qt.Key_Up)                                         # nothing above it
        self.assertTrue(bar.tab_btns[0].hasFocus())
        self.key(Qt.Key_Right)                                      # Video
        self.assertEqual(bar.tab_names[bar.tab], "Video")
        self.assertTrue(bar.tab_btns[1].hasFocus())
        self.assertEqual(bar.height(), h0 + self.PANEL + 1)        # no tab moves the bar
        self.key(Qt.Key_Down)
        self.assertTrue(bar.row("resolution").buttons[1].hasFocus())  # 1080p
        self.assertEqual([b.text() for b in bar.row("resolution").buttons],
                         ["720p", "1080p", "Native"])          # up to 1080p for now
        self.key(Qt.Key_Left)                    # 720p
        self.assertIn("4.5 GB for 60 min", bar.foot.text())
        self.key(Qt.Key_Down)                    # frame rate row (stays 60 fps)
        self.assertTrue(bar.row("fps").buttons[0].hasFocus())
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Right)                   # ultra
        self.key(Qt.Key_PageDown)                # Audio, on its first row
        self.assertEqual(bar.tab_names[bar.tab], "Audio")
        self.assertTrue(bar.row("audio_source").buttons[0].hasFocus())
        self.key(Qt.Key_Right)                   # first output
        self.key(Qt.Key_Down)
        h1 = bar.height()
        self.key(Qt.Key_Right)                   # mic on -> the device row appears...
        pump(self.app, 0.05)
        self.assertFalse(bar.row("mic_device").isHidden())
        self.assertEqual(bar.height(), h1)       # ...in room the panel already had
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
        self.key(Qt.Key_PageDown)                # another tab; the footer keeps focus
        self.assertEqual(bar.tab_names[bar.tab], "Controller")
        self.assertTrue(bar.apply_btn.hasFocus())
        self.key(Qt.Key_Up)                      # the tab's only row
        self.assertTrue(bar.row("controller").buttons[1].hasFocus())             # PS / Xbox + Down
        self.key(Qt.Key_PageUp)                  # back on Audio: Sound
        self.assertTrue(bar.row("audio_source").buttons[1].hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "settings", "settings")
        self.shot(bar, "settings", "v3")
        self.key(Qt.Key_Return)                  # Enter applies from anywhere, every tab at once
        pump(self.app, 0.05)
        self.assertEqual(bar.apply_state, "busy")
        self.assertIn("Applying", bar.foot.text())
        self.shot(bar, "applying", "settings")
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"resolution": "720p", "quality": "ultra",
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
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)
        bar.row("quality").buttons[0].click()   # touch / click
        self.assertEqual(bar.row("quality").value, "standard")
        self.assertEqual(bar.changes(), {"quality": "standard"})
        self.key(Qt.Key_Escape)                  # back to clips, nothing written
        self.assertEqual(bar.mode, "clip")
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)
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
        self.key(Qt.Key_PageDown)                # General -> Video
        self.key(Qt.Key_PageDown)                # -> Audio: Sound
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
        self.assertEqual(bar.name.text(), "Paused Full Screen")
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
        self.assertEqual(bar.confirm.text(), "Stop and clear replay?")   # keep_history off
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
        self.assert_stopped(bar)
        self.assertTrue(all(not o.isEnabled() for o in bar.options))   # the replay was cleared
        self.assertTrue(bar.running and bar.stopped)
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        bar.refresh_async()                       # the daemon itself now reports "stopped"
        self.wait_for(lambda: not bar.status_inflight)
        self.assertEqual(bar.last_status["state"], "stopped")
        self.assert_stopped(bar)
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

    def test_low_storage_warning_in_every_view(self):
        daemon = FakeDaemon(True, storage=SHORT_SPAN)
        bar = self.make(daemon)
        self.assertEqual(bar.view, "rec")                     # recording goes on
        self.assertFalse(bar.hintbar.isHidden())
        self.assertIn(SHORT_SPAN_MSG, bar.hintbar.text())
        self.assertIn(overlay.RED, bar.hintbar.text())        # a restart wouldn't fit either
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2 + overlay.HINT_H + 1)
        self.assertEqual(bar.storage_hint.level, "short")
        self.assertTrue(all(o.isEnabled() for o in bar.options[:5]))  # saving works as usual
        self.shot(bar, "warning-recording", "storage")
        daemon.paused = True                                  # paused: the warning replaces the paused hint
        bar.apply_status(daemon.request({"cmd": "status"}))
        self.assertEqual(bar.view, "paused")
        self.assertIn(SHORT_SPAN_MSG, bar.hintbar.text())
        self.assertNotIn(overlay.PAUSED_HINT, bar.hintbar.text())
        daemon.paused, daemon.stopped = False, True
        bar.apply_status(daemon.request({"cmd": "status"}))
        self.assertEqual(bar.view, "stopped")
        self.assertIn(SHORT_SPAN_MSG, bar.hintbar.text())
        pump(self.app, 0.1)                                   # let the layout settle for the picture
        self.shot(bar, "warning-stopped", "storage")
        # space freed: the strip goes away
        daemon.storage = OK_STORAGE
        bar.apply_status(daemon.request({"cmd": "status"}))
        self.assertTrue(bar.hintbar.isHidden())
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)

    def test_low_storage_warning_keep_history(self):
        bar = self.make(FakeDaemon(True, storage=SHORT_HISTORY, extra={"keep_history": True}))
        self.assertEqual(bar.view, "rec")
        text = bar.hintbar.text()
        self.assertIn("Low storage: 60 min at 1080p High with Keep history needs 15.4 GB, 11.3 GB free. "
                      "Free up space.", text)
        self.assertIn(overlay.YELLOW, text)                   # a restart still fits: a heads-up
        self.assertEqual(bar.storage_hint.level, "tight")
        self.shot(bar, "warning-keep-history", "storage")
        # an error or refusal shown later is red again
        bar.show_storage_warning("Not enough free space")
        self.assertIn(overlay.RED, bar.hintbar.text())

    def test_no_warning_when_the_span_fits(self):
        bar = self.make(FakeDaemon(True, storage={**SHORT_SPAN, "ok": True, "low": False, "free": 742e9,
                                                  "available": 742e9}))
        self.assertTrue(bar.hintbar.isHidden())
        # blocked: the daemon's own message wins over the low-storage line
        daemon = FakeDaemon(True, storage=SHORT_SPAN, extra={"state": "no_storage", "recording": False,
                                                             "error": LOW_ERROR})
        bar = self.make(daemon)
        self.assertEqual(bar.view, "lowstorage")
        self.assertIn("needs 7.2 GB, 3.1 GB free. Free up space", bar.hintbar.text())
        self.assertNotIn("60 min", bar.hintbar.text())

    def test_paused_resume_blocked_by_storage(self):
        daemon = FakeDaemon(True, paused=True, extra={"storage": LOW})
        bar = self.make(daemon)
        self.assertEqual(bar.view, "paused")
        self.key(Qt.Key_P)
        pump(self.app, 0.1)
        self.assertEqual(daemon.controls, [])     # resume never sent
        self.assertIn("Not enough free space", bar.hintbar.text())

    def test_resume_reply_no_storage(self):
        err = "1080p Ultra needs 18.9 GB free, 9.4 GB available"
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
        daemon = FakeDaemon(True, free=6e9, values={"resolution": "720p"})
        bar = self.make(daemon)
        self.open_settings(bar)
        res = bar.row("resolution")
        self.assertTrue(bar.apply_btn.isEnabled())
        self.assertIn("60 fps", bar.foot.text())
        self.assertEqual([b.property("nofit") for b in res.buttons], [False, True, True])
        self.key(Qt.Key_PageDown)                 # Video: Resolution
        self.key(Qt.Key_Right)                    # 1080p: 6.8 GB > 6.0 GB
        self.assertEqual(res.value, "1080p")
        self.assertFalse(bar.apply_btn.isEnabled())
        self.assertIn("Needs 6.8 GB · 6.0 GB free", bar.foot.text())
        self.assertEqual(bar.foot.kind, "warn")    # amber, with the warning glyph
        q = bar.row("quality")
        self.assertTrue(any(b.property("nofit") for b in q.buttons))
        self.key(Qt.Key_Return)                   # Enter does not apply
        pump(self.app, 0.1)
        self.assertEqual((bar.apply_state, daemon.configures), (None, []))
        for _ in range(4):                        # frame rate, quality, format, then the footer
            self.key(Qt.Key_Down)
        self.assertTrue(bar.back_btn.hasFocus())  # Apply is skipped
        self.key(Qt.Key_Left)
        self.assertTrue(bar.back_btn.hasFocus())
        res.buttons[1].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "lowstorage-settings", "v3")
        self.key(Qt.Key_Left)                     # back to 720p: fits again
        self.assertTrue(bar.apply_btn.isEnabled())
        self.assertNotIn("Needs", bar.foot.text())

    def test_settings_storage_lowering_always_allowed(self):
        # today's 1080p Ultra does not fit; a lower choice that still does not fit may be applied
        warn = "Not enough free space: needs 7.2 GB, 6.0 GB free"
        daemon = FakeDaemon(True, free=6e9, values={"resolution": "1080p", "quality": "ultra"},
                            configure_reply={"ok": True, "online": True, "state": "no_storage", "warning": warn})
        bar = self.make(daemon)
        self.open_settings(bar)
        self.assertIn("Needs", bar.foot.text())
        self.assertTrue(bar.apply_btn.isEnabled())         # no change is not a raise
        self.key(Qt.Key_PageDown)                           # Video: Resolution
        self.key(Qt.Key_Down)                               # frame rate
        self.key(Qt.Key_Right)                              # 120 fps: raises the requirement
        self.assertEqual(bar.row("fps").value, 120)
        self.assertFalse(bar.apply_btn.isEnabled())
        self.key(Qt.Key_Left)
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Left)                               # quality: high, still > 6.0 GB
        self.assertEqual(bar.row("quality").value, "high")
        self.assertFalse(bar.fits(bar.pending()))
        self.assertTrue(bar.apply_btn.isEnabled())
        daemon.extra = {"state": "no_storage", "recording": False, "error": warn,
                        "storage": {**LOW, "free": 6e9, "required": 7.2e9}}
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"quality": "high"}])
        self.assertIn("needs 7.2 GB", bar.foot.text())
        self.assertNotIn("recording restarted", bar.foot.text())
        self.wait_for(lambda: bar.mode == "clip", timeout=overlay.APPLY_CLOSE_MS / 1000 + 2)
        self.assertEqual(bar.view, "lowstorage")
        self.assertIn("needs 7.2 GB", bar.hintbar.text())

    # ---------------------------------------------------------------- resolution cap

    def screen(self, size):
        self.addCleanup(setattr, overlay, "SCREEN_SIZE", overlay.SCREEN_SIZE)
        overlay.SCREEN_SIZE = lambda: size

    def open_video(self, daemon):
        bar = self.make(daemon)
        self.open_settings(bar)
        bar.switch_tab(bar.tab_names.index("Video"), "row")
        pump(self.app, 0.05)
        return bar, bar.row("resolution")

    def test_120_fps_note(self):
        """Frame rate 120: a one-line note says when it helps; 60: no note."""
        bar, _res = self.open_video(FakeDaemon(True))
        fps = bar.row("fps")
        self.assertEqual((fps.value, fps.note.text()), (60, ""))
        self.key(Qt.Key_Down)                           # the Frame rate row
        self.key(Qt.Key_Right)                          # 120 fps
        self.assertEqual(fps.value, 120)
        self.assertEqual((fps.note.text(), fps.note.kind), (overlay.FPS_NOTE, "info"))
        self.assertEqual(fps.note.text(), "120 fps only helps if your game runs above 100 fps")
        self.assertIs(type(fps.note), type(bar.row("resolution").note))   # the same style
        self.assertFalse(fps.note.elided)                                 # fits the row, whole
        self.assertLessEqual(fps.note.geometry().right(), fps.width())
        self.assertGreaterEqual(fps.note.x(), fps.buttons[-1].geometry().right() + overlay.NOTE_GAP)
        pump(self.app, 0.05)
        self.shot(bar, "settings-video-120fps", "v7")
        self.key(Qt.Key_Left)                           # back to 60: the note goes
        self.assertEqual(fps.note.text(), "")
        bar2, _ = self.open_video(FakeDaemon(True, values={"fps": 120}))   # a saved 120: shown at once
        self.assertEqual(bar2.row("fps").note.text(), overlay.FPS_NOTE)

    def test_resolution_capped_by_the_screen(self):
        self.screen((1280, 720))                       # nothing recorded yet: the bar's own screen
        daemon = FakeDaemon(True, values={"resolution": "720p"})
        bar, res = self.open_video(daemon)
        self.assertEqual([b.text() for b in res.buttons], ["720p", "1080p", "Native"])
        self.assertEqual([b.isEnabled() for b in res.buttons], [True, False, True])
        self.assertEqual(res.buttons[1].visual_state, "disabled")
        self.assertEqual(res.disabled, {"1080p"})
        self.assertEqual(res.note.text(), "Your screen is 720p")
        self.assertTrue(res.buttons[0].hasFocus())     # 720p, the saved value
        self.assertIn("4.5 GB for 60 min", bar.foot.text())
        pump(self.app, 0.05)
        self.shot(bar, "resolution-720p-screen", "rescap")
        self.key(Qt.Key_Right)                          # 1080p is skipped
        self.assertEqual(res.value, "native")
        self.assertTrue(res.buttons[2].hasFocus())
        self.key(Qt.Key_Right)                          # the end: stays
        self.assertEqual(res.value, "native")
        self.key(Qt.Key_Left)
        self.assertEqual(res.value, "720p")
        self.key(Qt.Key_Left)                           # the start: stays
        self.assertEqual(res.value, "720p")
        self.key(Qt.Key_Right)
        QTest.mouseClick(res.buttons[1], Qt.LeftButton)  # a click on 1080p does nothing
        self.assertEqual(res.value, "native")
        self.assertEqual(res.note.text(), "Your screen is 720p")
        self.assertEqual(bar.changes(), {"resolution": "native"})
        self.assertFalse(any(b.property("nofit") for b in res.buttons))

    def test_resolution_on_a_1080p_screen(self):
        self.screen((1920, 1080))
        bar, res = self.open_video(FakeDaemon(True))
        self.assertEqual([b.text() for b in res.buttons], ["720p", "1080p", "Native"])
        self.assertTrue(all(b.isEnabled() for b in res.buttons))
        self.assertEqual(res.note.text(), "")           # nothing capped: no note
        pump(self.app, 0.05)
        self.shot(bar, "resolution-1080p-screen", "rescap")

    def test_resolution_on_a_4k_screen(self):
        self.screen((3840, 2160))
        bar, res = self.open_video(FakeDaemon(True))
        self.assertEqual([b.text() for b in res.buttons], ["720p", "1080p", "Native"])   # no 1440p / 4K
        self.assertTrue(all(b.isEnabled() for b in res.buttons))
        self.assertEqual(res.note.text(), "")           # nothing capped: no note
        pump(self.app, 0.05)
        self.shot(bar, "resolution-4k-screen", "rescap")
        self.key(Qt.Key_Right)
        self.assertEqual(res.value, "native")
        self.assertIn("6.8 GB for 60 min", bar.foot.text())   # native records 1080 lines here

    def test_daemon_source_wins_over_the_screen(self):
        self.screen((1280, 720))
        _bar, res = self.open_video(FakeDaemon(True, source=[3840, 2160]))
        self.assertTrue(all(b.isEnabled() for b in res.buttons))
        self.screen((3840, 2160))
        _bar, res = self.open_video(FakeDaemon(True, source=[1280, 720],
                                               values={"record": "screen", "resolution": "720p"}))
        self.assertEqual([b.isEnabled() for b in res.buttons], [True, False, True])
        self.assertEqual(res.note.text(), "Your screen is 720p")

    def test_resolution_saved_above_the_screen(self):
        # another monitor: 1080p saved, a 720p screen recorded
        daemon = FakeDaemon(True, values={"record": "screen"}, source=[1280, 720])
        bar, res = self.open_video(daemon)
        full_hd = res.buttons[1]
        self.assertEqual(res.value, "1080p")
        self.assertFalse(full_hd.isEnabled())
        self.assertEqual(full_hd.visual_state, "capped")  # still shown as chosen, dimmed
        self.assertEqual(res.note.text(), "Recording at 720p (your screen)")
        self.assertTrue(res.buttons[0].hasFocus())      # the nearest choice that applies: 720p
        self.assertIn("4.5 GB for 60 min", bar.foot.text())   # what is really recorded, not 1080p's 6.8 GB
        self.assertEqual(bar.changes(), {})
        pump(self.app, 0.05)
        self.shot(bar, "resolution-1080p-saved-720p-screen", "rescap")
        self.key(Qt.Key_Left)                           # the step toward it chooses it
        self.assertEqual(res.value, "720p")
        self.assertEqual(full_hd.visual_state, "disabled")
        self.assertEqual(res.note.text(), "Your screen is 720p")
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Up)                              # back on the row: the chosen value
        self.assertTrue(res.buttons[0].hasFocus())
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"resolution": "720p"}])

    def test_resolution_saved_above_the_screen_left_alone(self):
        daemon = FakeDaemon(True, values={"resolution": "1080p", "record": "screen"}, source=[1280, 720])
        bar, res = self.open_video(daemon)
        self.assertTrue(res.buttons[0].hasFocus())      # the nearest choice: 720p (not chosen)
        self.assertEqual(res.value, "1080p")
        self.key(Qt.Key_Down)                           # other rows: the saved 1080p stays
        self.key(Qt.Key_Right)                          # 120 fps
        self.assertEqual(bar.changes(), {"fps": 120})
        self.key(Qt.Key_Up)
        self.key(Qt.Key_Right)                          # from the saved 1080p: Native
        self.assertEqual(res.value, "native")
        self.assertEqual(res.note.text(), "Your screen is 720p")

    def test_resolution_capped_by_the_window(self):
        daemon = FakeDaemon(True, values={"record": "window"}, source=[1280, 720], extra={"target": "window"})
        bar, res = self.open_video(daemon)
        self.assertEqual([b.isEnabled() for b in res.buttons], [True, False, True])
        self.assertEqual(res.note.text(), "Recording at 1280\u00d7720 (window size)")   # 1080p saved
        self.assertTrue(res.buttons[0].hasFocus())      # the nearest choice: 720p
        self.key(Qt.Key_Left)                           # the step toward it chooses it
        self.assertEqual(res.value, "720p")
        self.assertEqual(res.note.text(), "Window is 1280\u00d7720")
        pump(self.app, 0.05)
        self.shot(bar, "resolution-720p-window", "rescap")

    def test_resolution_note_fits_the_row(self):
        daemon = FakeDaemon(True, values={"resolution": "1080p", "record": "window"}, source=[3440, 1050])
        bar, res = self.open_video(daemon)
        self.assertEqual(res.note.text(), "Recording at 3440\u00d71050 (window size)")
        pump(self.app, 0.05)
        right = res.note.mapTo(bar, res.note.rect().topRight()).x()
        self.assertLessEqual(right, bar.width())
        self.assertGreaterEqual(res.note.x(), res.buttons[-1].x() + res.buttons[-1].width())
        self.assertFalse(res.note.elided)                                 # not squeezed

    # ---------------------------------------------------------------- video format

    def open_format(self, values=None, **reply):
        """Settings -> Video with a settings reply that says what this machine records (``reply``)."""
        daemon = FakeDaemon(True, values=values)
        real = daemon.request

        def request(msg, timeout=120, **kw):
            r = real(msg, timeout=timeout, **kw)
            if msg["cmd"] == "settings":
                r.update(reply)
            return r
        daemon.request = request
        bar = self.make(daemon)
        self.open_settings(bar)
        bar.switch_tab(bar.tab_names.index("Video"), "row")
        pump(self.app, 0.05)
        return bar, bar.row("format"), daemon

    def test_format_row_on_a_machine_that_records_everything(self):
        bar, fmt, _d = self.open_format(format_allowed=["auto", "h264", "h265", "av1"], format_auto="av1")
        self.assertIsNotNone(fmt)
        self.assertEqual([b.text() for b in fmt.buttons], ["Auto", "H.264", "H.265", "AV1"])   # just "Auto"
        self.assertTrue(all(b.isEnabled() for b in fmt.buttons))
        self.assertEqual(fmt.value, "auto")
        self.assertEqual(fmt.note.text(), "Recording in AV1 on this PC")   # what Auto uses, said plainly
        fmt.buttons[1].setFocus()                                   # moving along the row explains each
        self.assertEqual(fmt.note.text(), "Plays everywhere")
        fmt.buttons[3].setFocus()
        self.assertEqual(fmt.note.text(), "Smoothest on newer hardware. Some older devices can't play it")
        fmt.buttons[0].setFocus()                                   # back on Auto: what it records in
        self.assertEqual(fmt.note.text(), "Recording in AV1 on this PC")
        fmt.buttons[2].setFocus()
        bar.row("resolution").focus()                               # away from the row: the chosen one (Auto)
        self.assertEqual(fmt.note.text(), "Recording in AV1 on this PC")
        pump(self.app, 0.05)
        self.shot(bar, "format-auto-av1", "format")
        right = fmt.note.mapTo(bar, fmt.note.rect().topRight()).x()
        self.assertLessEqual(right, bar.width())                    # the hint fits the row
        self.assertGreaterEqual(fmt.note.x(), fmt.buttons[-1].x() + fmt.buttons[-1].width())

    def test_format_row_disables_what_the_chip_cant_record(self):
        bar, fmt, daemon = self.open_format(format_allowed=["auto", "h264", "h265"], format_auto="h264")
        self.assertEqual([b.text() for b in fmt.buttons], ["Auto", "H.264", "H.265", "AV1"])
        self.assertEqual([b.isEnabled() for b in fmt.buttons], [True, True, True, False])
        self.assertEqual(fmt.note.text(), "Your graphics chip can't record AV1")   # why AV1 is greyed
        fmt.buttons[0].setFocus()
        self.assertEqual(fmt.note.text(), "Recording in H.264 on this PC")
        bar.row("resolution").focus()
        pump(self.app, 0.05)
        self.shot(bar, "format-no-av1", "format")
        QTest.mouseClick(fmt.buttons[3], Qt.LeftButton)              # AV1: nothing happens
        self.assertEqual(fmt.value, "auto")
        QTest.mouseClick(fmt.buttons[2], Qt.LeftButton)
        self.assertEqual(fmt.value, "h265")
        self.assertEqual(fmt.note.text(), "Smaller files. Some older devices can't play it")
        self.assertEqual(bar.changes(), {"format": "h265"})
        self.assertEqual(bar.note.text(), "Applying restarts recording · your replay is kept")
        fmt.focus()
        self.key(Qt.Key_Right)                                       # AV1 is skipped: the end, stays
        self.assertEqual(fmt.value, "h265")
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"format": "h265"}])

    def test_format_saved_that_the_chip_cant_record(self):
        _bar, fmt, _d = self.open_format(values={"format": "av1"}, format_allowed=["auto", "h264"],
                                         format_auto="h264")
        self.assertEqual(fmt.value, "av1")
        self.assertEqual(fmt.buttons[3].visual_state, "capped")    # still shown as chosen, dimmed
        self.assertEqual(fmt.note.text(), "Your graphics chip can't record H.265 or AV1")

    def test_format_row_greys_a_format_that_crashed(self):
        # AV1 crashed Momento here (a driver abort): greyed with the reason, still choosable
        bar, fmt, daemon = self.open_format(values={"format": "av1"}, format_allowed=["auto", "h264", "h265", "av1"],
                                            format_auto="h264", format_effective="h265", format_crashed=["av1"])
        self.assertEqual([b.isEnabled() for b in fmt.buttons], [True, True, True, True])
        self.assertEqual([bool(b.property("dim")) for b in fmt.buttons], [False, False, False, True])
        self.assertEqual(fmt.value, "av1")
        self.assertEqual(fmt.buttons[3].visual_state, "capped")     # the saved one, shown dimmed
        self.assertEqual((fmt.note.text(), fmt.note.kind), ("AV1 stopped working here. Pick it again to retry", "warn"))
        fmt.buttons[2].setFocus()
        self.assertEqual(fmt.note.text(), "Smaller files. Some older devices can't play it")
        fmt.buttons[3].setFocus()
        self.assertEqual((fmt.note.text(), fmt.note.kind), ("AV1 stopped working here. Pick it again to retry", "warn"))
        bar.row("resolution").focus()
        pump(self.app, 0.05)
        self.shot(bar, "format-crashed", "format")
        self.assertEqual(bar.changes(), {})
        QTest.mouseClick(fmt.buttons[3], Qt.LeftButton)              # picked again: a retry
        self.assertEqual(fmt.value, "av1")
        self.assertFalse(fmt.buttons[3].property("dim"))
        self.assertEqual(bar.changes(), {"format": "av1"})           # sent although it is the saved value
        self.assertEqual(bar.note.text(), "Applying restarts recording · your replay is kept")
        QTest.mouseClick(fmt.buttons[1], Qt.LeftButton)              # changed their mind
        self.assertEqual(bar.changes(), {"format": "h264"})
        self.assertTrue(fmt.buttons[3].property("dim"))
        QTest.mouseClick(fmt.buttons[3], Qt.LeftButton)
        self.assertEqual(bar.changes(), {"format": "av1"})
        bar.apply_settings()
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"format": "av1"}])

    def test_format_row_crash_note_with_auto(self):
        _bar, fmt, _d = self.open_format(format_allowed=["auto", "h264", "h265", "av1"], format_auto="h264",
                                         format_crashed=["av1", "h265"])
        self.assertEqual(fmt.value, "auto")
        self.assertEqual(fmt.note.text(), "H.265 and AV1 stopped working here. Pick one again to retry")
        fmt.buttons[0].setFocus()
        self.assertEqual(fmt.note.text(), "Recording in H.264 on this PC")

    def test_format_auto_says_what_is_really_recorded(self):
        # Auto saved: the daemon's format_effective (what the recorder really uses) wins
        bar, fmt, _d = self.open_format(format_allowed=["auto", "h264", "h265", "av1"], format_auto="av1",
                                        format_effective="h265")
        self.assertEqual(fmt.buttons[0].text(), "Auto")
        self.assertEqual(fmt.note.text(), "Recording in H.265 on this PC")
        self.app.removeEventFilter(bar)
        bar.close()
        pump(self.app, 0.05)
        # another format saved: format_effective is that one's, Auto's note keeps format_auto
        _bar, fmt, _d = self.open_format(values={"format": "h264"}, format_allowed=["auto", "h264", "h265", "av1"],
                                         format_auto="av1", format_effective="h264")
        self.assertEqual(fmt.note.text(), "Plays everywhere")
        fmt.buttons[0].setFocus()
        self.assertEqual(fmt.note.text(), "Recording in AV1 on this PC")

    # ---------------------------------------------------------------- readable notes

    def test_informative_text_reads_at_wcag_aa(self):
        self.assertAlmostEqual(overlay.contrast("#FFFFFF", "#000000"), 21.0, places=2)
        solid = "#{:02X}{:02X}{:02X}".format(*overlay.BG[:3])
        worst = overlay.bar_background("#FFFFFF")      # the bar over a white game
        self.assertEqual(worst, "#1F1F1F")
        for name in ("TEXT", "NOTE", "WARN", "RED", "MUTED"):   # MUTED: labels, the gallery's hints and meta
            for bg in (solid, worst):
                self.assertGreaterEqual(overlay.contrast(getattr(overlay, name), bg), overlay.READABLE,
                                        f"{name} on {bg}")
        self.assertLess(overlay.contrast(overlay.DIM, solid), overlay.READABLE)   # DIM: disabled only
        for kind, color in overlay.NOTE_COLORS.items():
            self.assertNotEqual(color, overlay.DIM, kind)
        # the note colour sits between the labels and the primary text
        self.assertLess(overlay.contrast(overlay.MUTED, solid), overlay.contrast(overlay.NOTE, solid))
        self.assertLess(overlay.contrast(overlay.NOTE, solid), overlay.contrast(overlay.TEXT, solid))

    def test_note_crossfades_without_blocking(self):
        bar, fmt, _d = self.open_format(format_allowed=["auto", "h264", "h265", "av1"], format_auto="av1")
        note = fmt.note
        pump(self.app, 0.3)
        self.assertIsNone(note.old)
        t0 = time.monotonic()
        fmt.buttons[3].setFocus()                                   # AV1: its one-liner
        self.assertLess(time.monotonic() - t0, 0.05)                # nothing waits for the fade
        self.assertEqual(note.text(), "Smoothest on newer hardware. Some older devices can't play it")
        self.assertEqual(note.old[0], "Recording in AV1 on this PC")   # the old text fading out
        self.assertLess(note._t, 1.0)
        fmt.buttons[0].setFocus()                                   # a change mid-fade: no queue
        self.assertEqual(note.text(), "Recording in AV1 on this PC")
        self.wait_for(lambda: note.old is None, timeout=1)
        self.assertEqual(note._t, 1.0)
        self.assertLessEqual(overlay.NOTE_FADE_MS, 150)
        self.addCleanup(setattr, overlay, "ANIMATE", overlay.ANIMATE)
        overlay.ANIMATE = False                                     # reduced motion: at once
        fmt.buttons[1].setFocus()
        self.assertEqual((note.text(), note.old, note._t), ("Plays everywhere", None, 1.0))

    def test_note_kinds_and_places(self):
        bar, fmt, _d = self.open_format(format_allowed=["auto", "h264", "h265"], format_auto="h264")
        self.assertEqual((fmt.note.text(), fmt.note.kind), ("Your graphics chip can't record AV1", "warn"))
        fmt.buttons[0].setFocus()
        self.assertEqual((fmt.note.text(), fmt.note.kind), ("Recording in H.264 on this PC", "info"))
        self.assertEqual((bar.note.kind, bar.foot.kind), ("plain", "plain"))   # status lines: no glyph
        pump(self.app, 0.3)
        # every note at its row's end, centred with the pills, clear of them
        for row in bar.visible_rows():
            n, last = row.note, row.buttons[-1] if not row.cycle else row.next
            self.assertGreaterEqual(n.x(), last.geometry().right() + overlay.NOTE_GAP, row.key)
            self.assertLessEqual(n.geometry().right(), row.width() - 12, row.key)
            self.assertEqual(n.geometry().center().y(), last.geometry().center().y(), row.key)
        header_right = bar.note.mapTo(bar, bar.note.rect().topRight()).x()
        self.assertLessEqual(header_right, bar.width())
        self.assertGreater(bar.note.x(), bar.tab_btns[-1].geometry().right())
        # too long for the row: two lines, then an ellipsis and the whole text as a tooltip
        width = fmt.note.width()
        long = "This note is far too long for the room at the end of the Format row " * 3
        fmt.note.set(long.strip(), "info", animate=False)
        lines, elided = fmt.note.layout_lines(fmt.note.state)
        self.assertEqual((len(lines), elided), (2, True))
        self.assertTrue(lines[-1].endswith("…"))
        self.assertEqual(fmt.note.toolTip(), long.strip())
        self.assertEqual(fmt.note.width(), width)                   # never wider: the pills stay put
        fmt.note.set("Plays everywhere", "info", animate=False)
        self.assertEqual((fmt.note.elided, fmt.note.toolTip()), (False, ""))
        self.shot(bar, "notes-kinds", "notes")

    def test_shorter_replay_is_a_warning(self):
        bar = self.make(FakeDaemon(True))
        self.open_settings(bar)
        rl = bar.row("replay_length")
        rl.select(rl.values.index(15))
        self.assertEqual((bar.note.text(), bar.note.kind),
                         ("Keeps the newest 15 min · older footage is dropped", "warn"))
        rl.select(rl.values.index(60))
        self.assertEqual((bar.note.text(), bar.note.kind),
                         ("Applying restarts recording · your replay is kept", "plain"))

    def test_format_row_from_an_older_daemon(self):
        # no format_* fields: every format offered, Auto without a pick
        _bar, fmt, _d = self.open_format()
        self.assertEqual([b.text() for b in fmt.buttons], ["Auto", "H.264", "H.265", "AV1"])
        self.assertTrue(all(b.isEnabled() for b in fmt.buttons))
        self.assertEqual(fmt.note.text(), "Picks a format your PC records well")

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
        for _ in range(7):
            self.key(Qt.Key_Tab)                             # 60m -> pause, stop, screenshot, gear
        self.assertEqual(bar.gear.visual_state, "focus")
        self.assertEqual(bar.options[4].visual_state, "rest")

    def test_settings_selected_and_focused_pills(self):
        bar = self.make(FakeDaemon(True, free=9.4e9))
        self.open_settings(bar)
        res, fps, qual = bar.row("resolution"), bar.row("fps"), bar.row("quality")
        self.key(Qt.Key_PageDown)                                    # Video: Resolution
        self.assertEqual(res.buttons[1].visual_state, "focus")      # 1080p: selected + focused
        self.assertEqual(qual.buttons[qual.idx].visual_state, "selected")
        self.assertEqual(fps.buttons[fps.idx].visual_state, "selected")
        self.assertEqual(res.buttons[0].visual_state, "rest")
        self.assertTrue(qual.buttons[2].property("nofit"))           # Ultra: 11.3 GB > 9.4, still marked
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
        self.assertEqual((overlay.IDLE_HIDE_MS, overlay.LEAVE_HIDE_MS, overlay.GALLERY_IDLE_MS),
                         (3_000, 500, 10_000))

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
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)
        bar.ask_stop()
        pump(self.app, 0.05)
        self.shot(bar, "stop-confirm-icons", "v5")
        t0 = time.monotonic()
        bar.confirm_stop()
        self.wait_for(lambda: daemon.controls == ["stop"] and not bar.control_busy)
        self.assertEqual(bar.view, "stopped")     # the stopped state shows briefly...
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
        daemon = FakeDaemon(True, values={"record": "screen"})
        bar = self.make(daemon)
        self.open_settings(bar)
        row = bar.row("record")
        self.assertIs(bar.rows[0], row)
        self.assertEqual(bar.tab_names[bar.tab], "General")
        self.assertEqual([b.text() for b in row.buttons], ["Full screen", "Window"])
        self.assertEqual((row.value, row.icon.kind), ("screen", "fullscreen"))
        xs = {r.icon.mapTo(bar, r.icon.rect().topLeft()).x() for r in bar.visible_rows()}
        self.assertEqual(len(xs), 1)             # one icon column
        self.assertLessEqual(row.sizeHint().width(), bar.width())
        self.assertTrue(row.buttons[0].hasFocus())
        pump(self.app, 0.05)
        self.shot(bar, "settings-record", "v5")
        self.key(Qt.Key_Right)                                       # Window
        self.assertEqual((row.value, row.icon.kind), ("window", "window"))
        self.assertEqual(bar.changes(), {"record": "window"})
        pump(self.app, 0.05)
        self.shot(bar, "settings-record-window", "v5")
        bar.resident = True
        self.key(Qt.Key_Return)
        self.wait_for(lambda: daemon.configures == [{"record": "window"}])
        # the window picker opens now: the bar gets out of its way right after the reply
        self.wait_for(lambda: not bar.isVisible(), timeout=2)

    def test_no_change_window(self):
        """Picking a window happens only through play: the Record row is just its two values."""
        daemon = FakeDaemon(True, values={"record": "window"}, extra={"target": "window"})
        bar = self.make(daemon)
        w0 = bar.width()
        self.open_settings(bar)
        row = bar.row("record")
        self.assertEqual((row.value, len(row.buttons)), ("window", 2))
        self.assertFalse(hasattr(bar, "change_btn"))
        texts = [b.text() for b in bar.pills()]
        self.assertFalse(any("Change" in t for t in texts), texts)
        self.assertEqual(bar.width(), w0)
        self.assertTrue(row.buttons[1].hasFocus())
        self.key(Qt.Key_Right)                                       # nothing past Window
        self.assertTrue(row.buttons[1].hasFocus())
        self.key(Qt.Key_Return)                                      # nothing changed: back
        self.assertEqual(bar.mode, "clip")
        self.assertEqual((daemon.controls, daemon.configures), ([], []))

    # ---------------------------------------------------------------- settings tabs

    def tab_shots(self, bar, prefix="v7"):
        for i, name in enumerate(bar.tab_names):
            bar.switch_tab(i, "row")          # focus on the first row: the tab shows as open
            pump(self.app, 0.05)
            self.shot(bar, f"settings-tab-{name.lower()}", prefix)

    def test_settings_tabs(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.move(100, 700)
        h0, bottom0 = bar.height(), bar.y() + bar.height()
        self.open_settings(bar)
        self.assertEqual(bar.tab_names, ["General", "Video", "Audio", "Controller", "Misc"])
        self.assertEqual([b.text() for b in bar.tab_btns], bar.tab_names)
        self.assertEqual([b.visual_state for b in bar.tab_btns],
                         ["selected", "rest", "rest", "rest", "rest"])
        for b in bar.tab_btns:                                       # small pills, one row
            self.assertEqual(b.pill_rect().height(), overlay.TAB_PILL_H)
            self.assertEqual(b.mapTo(bar, b.rect().topLeft()).y(), bar.tab_btns[0].mapTo(bar, b.rect().topLeft()).y())
        want = {"General": ["record", "replay_length", "keep_history"],
                "Video": ["resolution", "fps", "quality", "format"],
                "Audio": ["audio_source", "mic", "mic_device"],
                "Controller": ["controller"],
                "Misc": ["hour_warning", "instant_bar"]}
        heights = set()
        for i, name in enumerate(bar.tab_names):
            bar.switch_tab(i, "row")
            pump(self.app, 0.02)
            self.assertEqual([r.key for r in bar.rows if r.tab == i], want[name])
            shown = bar.visible_rows()
            self.assertTrue(all(r.isVisible() for r in shown))
            self.assertTrue(all(not r.isVisible() for r in bar.rows if r.tab != i))
            self.assertTrue(shown[0].buttons[shown[0].idx if not shown[0].cycle else 0].hasFocus()
                            or shown[0].isAncestorOf(self.app.focusWidget()))
            heights.add((bar.height(), bar.y() + bar.height()))
        self.assertEqual(heights, {(h0 + self.PANEL + 1, bottom0)})  # one size for every tab, grown upward
        self.tab_shots(bar)
        # the bumpers' keys: Page Up / Page Down, clamped at the ends
        bar.switch_tab(0, "row")
        self.key(Qt.Key_PageUp)
        self.assertEqual(bar.tab, 0)
        for _ in range(6):
            self.key(Qt.Key_PageDown)
        self.assertEqual(bar.tab_names[bar.tab], "Misc")
        self.assertTrue(bar.row("hour_warning").buttons[0].hasFocus())
        # on the tab row Left / Right switch tabs and Enter goes into the tab
        self.key(Qt.Key_Up)
        self.assertTrue(bar.tab_btns[4].hasFocus())
        self.key(Qt.Key_Right)                                       # the last tab: stays
        self.assertEqual(bar.tab, 4)
        self.key(Qt.Key_Left)
        self.assertEqual((bar.tab_names[bar.tab], bar.tab_btns[3].hasFocus()), ("Controller", True))
        self.assertEqual(bar.tab_btns[3].visual_state, "focus")
        pump(self.app, 0.05)
        self.shot(bar, "settings-tab-focus", "v7")
        self.key(Qt.Key_Return)
        self.assertTrue(bar.row("controller").isAncestorOf(self.app.focusWidget()))
        self.assertEqual(bar.tab_btns[3].visual_state, "selected")
        # a click on a tab opens it
        QTest.mouseClick(bar.tab_btns[1], Qt.LeftButton)
        self.assertEqual(bar.tab_names[bar.tab], "Video")
        self.assertIsNotNone(self.app.focusWidget())
        self.assertTrue(self.app.focusWidget().isVisible())

    def test_settings_dividers(self):
        bar = self.make(FakeDaemon(True))
        self.open_settings(bar)
        bar.switch_tab(bar.tab_names.index("Video"), "row")
        pump(self.app, 0.05)
        rows = bar.visible_rows()
        self.assertEqual([r.has_divider() for r in rows], [False, True, True, True])   # between rows only
        bar.settle()
        img = bar.grab().toImage()
        for r in rows:
            y = r.mapTo(bar, r.rect().topLeft()).y()
            line = img.pixelColor(bar.width() // 2, y)
            above = img.pixelColor(bar.width() // 2, y + 3)
            if r.has_divider():
                self.assertNotEqual(line.name(), above.name())               # a hairline...
                self.assertLess(abs(line.lightness() - above.lightness()), 40)  # ...a subtle one
            self.assertEqual(img.pixelColor(8, y).name(), above.name())     # inset from the edges
        # the Audio tab: Mic device shows (and gets its line) only with the mic on
        bar.switch_tab(bar.tab_names.index("Audio"), "row")
        self.assertEqual([r.has_divider() for r in bar.visible_rows()], [False, True])
        bar.row("mic").buttons[1].click()
        self.assertEqual([r.has_divider() for r in bar.visible_rows()], [False, True, True])
        self.assertFalse(bar.row("mic_device").isHidden())

    def test_settings_new_rows(self):
        daemon = FakeDaemon(True, values={"hour_warning": 7})     # set by hand in the config
        bar = self.make(daemon)
        self.open_settings(bar)
        kh = bar.row("keep_history")
        self.assertEqual(([b.text() for b in kh.buttons], kh.value, kh.icon.kind),
                         (["Off", "On"], "off", "history"))
        hw, ib = bar.row("hour_warning"), bar.row("instant_bar")
        self.assertEqual(([b.text() for b in hw.buttons], hw.value, hw.icon.kind),
                         (["10 min", "5 min", "3 min", "7 min"], 7, "hourglass"))
        self.assertEqual(([b.text() for b in ib.buttons], ib.value, ib.icon.kind),
                         (["On", "Off"], "on", "bolt"))
        self.assertEqual([r.findChild(QLabel).text() for r in bar.rows],
                         ["Record", "Replay length", "Keep history", "Resolution", "Frame rate", "Quality",
                          "Format", "Sound", "Mic",
                          "Mic device", "Controller", "Hour warning", "Instant bar"])
        for r in bar.rows:                         # every title fits its column
            lbl = r.findChild(QLabel)
            self.assertLessEqual(lbl.fontMetrics().horizontalAdvance(lbl.text()), lbl.width())
        # changes on two tabs go out together with one Apply
        self.key(Qt.Key_Down)                      # General: Replay length
        self.key(Qt.Key_Down)                      # Keep history
        self.key(Qt.Key_Right)                     # On
        for _ in range(4):
            self.key(Qt.Key_PageDown)              # Misc: Hour warning
        self.key(Qt.Key_Right)                     # 7 min is the last choice: stays
        self.key(Qt.Key_Left)                      # 3 min
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Right)                     # Instant bar: Off
        self.assertEqual(bar.changes(), {"keep_history": "on", "hour_warning": 3, "instant_bar": "off"})
        daemon.configure_reply = {"ok": True, "changed": bar.changes(), "restarted": False, "paused": False}
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"keep_history": "on", "hour_warning": 3, "instant_bar": "off"}])
        self.assertEqual(bar.foot.text(), "Saved")            # nothing restarted
        self.assertNotEqual((bar.last_status or {}).get("state"), "starting")

    def test_settings_controller_tab(self):
        """Controller tab: one row, Off / PS / Xbox + Down (Open with and Exclusive are gone);
        applies without a restart."""
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.move(100, 700)
        h0, bottom0 = bar.height(), bar.y() + bar.height()
        self.open_settings(bar)
        bar.switch_tab(bar.tab_names.index("Controller"), "row")
        pump(self.app, 0.05)
        self.assertEqual([r.key for r in bar.visible_rows()], ["controller"])
        self.assertEqual(bar.panel_rows, 4)                       # the tallest tab (Video) sets the panel
        self.assertEqual((bar.height(), bar.y() + bar.height()), (h0 + self.PANEL + 1, bottom0))
        row = bar.row("controller")
        self.assertEqual((row.findChild(QLabel).text(), [b.text() for b in row.buttons], row.value, row.icon.kind),
                         ("Controller", ["Off", "PS / Xbox + Down"], "ps_down", "gamepad"))
        self.shot(bar, "settings-controller-row", "controller")
        self.assertTrue(row.buttons[1].hasFocus())                # the tab opened on its row
        self.key(Qt.Key_Right)                                    # the last choice: stays
        self.assertEqual(row.value, "ps_down")
        self.key(Qt.Key_Left)                                     # Off
        self.assertEqual(bar.changes(), {"controller": "off"})
        daemon.configure_reply = {"ok": True, "changed": {"controller": "off"}, "restarted": False,
                                  "paused": False}
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"controller": "off"}])
        self.assertIn("controller updated", bar.foot.text())
        self.assertNotEqual((bar.last_status or {}).get("state"), "starting")   # nothing restarted

    def test_settings_controller_older_shortcuts(self):
        """A saved shortcut the row doesn't offer (an older preset, a hand-edited list) is an
        extra choice named by its buttons; without python-evdev there is no row."""
        for value, label in (("view_menu", "View + Menu"), ("left_paddle", "Left paddle"),
                             ("l3_r3", "L3 + R3"), ("tl+tr", "LB + RB")):
            bar = self.make(FakeDaemon(True, values={"controller": value}))
            self.open_settings(bar)
            row = bar.row("controller")
            self.assertEqual(([b.text() for b in row.buttons], row.value),
                             (["Off", "PS / Xbox + Down", label], value), value)
            self.assertTrue(row.buttons[2].selected())
        bar.clear_rows()
        data = settings_reply()                                   # no python-evdev: no controller rows
        data["controller_available"] = False
        bar.sdata = data
        bar.build_rows()
        self.assertEqual([r.key for r in bar.rows if r.key.startswith("controller")], [])
        self.assertTrue(bar.apply_btn.isEnabled())

    def test_settings_older_reply_without_tabs(self):
        """A reply without "tabs" or the new keys: the default tabs, holding what exists."""
        data = settings_reply()
        data.pop("tabs")
        for k in NEW_VALUES:
            data["values"].pop(k)
        bar = self.make(FakeDaemon(True))
        bar.sdata = data
        bar.build_rows()
        self.assertEqual(bar.tab_names, ["General", "Video", "Audio", "Controller"])   # Misc is empty
        self.assertEqual([r.key for r in bar.rows if r.tab == 0], ["record"])
        bar.clear_rows()

    # ---------------------------------------------------------------- the label next to the time

    def test_recording_labels(self):
        cases = [({"target": "window", "target_name": "Elden Ring"}, "Recording Elden Ring", "recording-window"),
                 ({"target": "window", "target_name": None}, "Recording Window", "recording-window-noname"),
                 ({"target": "screen"}, "Recording Full Screen", "recording-fullscreen"),
                 ({}, "Recording Full Screen", None)]                # an older daemon: no target
        widths = set()
        for extra, text, shot in cases:
            bar = self.make(FakeDaemon(True, extra=extra))
            self.assertEqual((bar.view, bar.name.text(), bar.name.accessibleName()), ("rec", text, text))
            self.assertFalse(bar.name.isHidden())
            name_x = bar.name.mapTo(bar, bar.name.rect().topLeft()).x()
            self.assertLess(bar.logo.mapTo(bar, bar.logo.rect().topLeft()).x(), name_x)
            self.assertLess(name_x, bar.time.mapTo(bar, bar.time.rect().topLeft()).x())   # label, then time
            widths.add((bar.width(), tuple(o.mapTo(bar, o.rect().topLeft()).x() for o in bar.options)))
            if shot:
                self.shot(bar, shot, "v7")
        self.assertEqual(len(widths), 1)            # the label never moves the clip lengths
        self.assertLess(bar.width(), 1050)          # still a slim, inline bar (1040 with the gallery button)
        # a long title is elided; markup in a title is shown as text
        title = "The Elder Scrolls V: Skyrim Special Edition — Anniversary Upgrade"
        bar = self.make(FakeDaemon(True, extra={"target": "window", "target_name": title}))
        self.assertTrue(bar.name.text().startswith("Recording The "))
        self.assertTrue(bar.name.text().endswith("…"))
        self.assertLessEqual(bar.name.fontMetrics().horizontalAdvance(bar.name.text()), bar.name_w)
        self.assertEqual(bar.name.accessibleName(), f"Recording {title}")
        self.assertEqual(bar.width(), widths.pop()[0])
        self.shot(bar, "recording-window-long", "v7")
        bar = self.make(FakeDaemon(True, extra={"target": "window", "target_name": "<b>Q</b>\n"}))
        self.assertEqual((bar.name.textFormat(), bar.name.text()), (Qt.PlainText, "Recording <b>Q</b>"))

    def test_paused_labels(self):
        for extra, text in (({"target": "window", "target_name": "Elden Ring"}, "Paused Elden Ring"),
                            ({"target": "window"}, "Paused Window"),
                            ({"target": "screen"}, "Paused Full Screen")):
            bar = self.make(FakeDaemon(True, paused=True, extra=extra))
            self.assertEqual((bar.view, bar.name.text()), ("paused", text))
            self.assertEqual(bar.time.text(), "12:34")
        self.shot(bar, "paused-fullscreen", "v7")
        bar = self.make(FakeDaemon(True, paused=True,
                                   extra={"target": "window", "target_name": "Baldur's Gate 3 (Vulkan) — Act III"}))
        self.assertTrue(bar.name.text().startswith("Paused Baldur"))
        self.assertTrue(bar.name.text().endswith("…"))

    # ---------------------------------------------------------------- stopped

    STOPPED = {"state": "stopped", "recording": False, "buffered": 0, "stop_reason": "window_closed",
               "keep_history": False}

    def test_stopped_window(self):
        ref = self.make(FakeDaemon(True))                            # a recording bar, for its width
        rec_w = ref.width()
        daemon = FakeDaemon(True, extra={**self.STOPPED, "target": "window", "target_name": "Elden Ring"})
        bar = self.make(daemon)
        self.assert_stopped(bar, "window")
        self.assertEqual(bar.name.text(), "Press play to pick a window")
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)      # no strip above: one sentence
        self.assertEqual(bar.width(), rec_w)                         # same bar, same width
        self.assertTrue(all(not o.isEnabled() for o in bar.options)) # nothing kept to save
        play = bar.controls[0]["pause"]
        self.assertEqual(play.accessibleName(), "Pick a window (P)")
        self.assertTrue(play.hasFocus())                             # play is the next step
        self.assertFalse(bar.live_ticking)
        self.shot(bar, "stopped-window", "v7")
        bar.resident = True
        self.key(Qt.Key_P)                                           # play: the daemon opens the picker
        self.wait_for(lambda: daemon.controls == ["resume"])
        self.wait_for(lambda: not bar.isVisible(), timeout=2)       # the picker dialog gets the screen
        self.assertNotIn("pick_window", daemon.controls)

    def test_stopped_screen(self):
        daemon = FakeDaemon(True, extra={**self.STOPPED, "stop_reason": "user", "target": "screen"})
        bar = self.make(daemon)
        self.assert_stopped(bar, "screen")
        self.assertEqual(bar.name.text(), "Press play to record full screen")
        self.assertEqual(bar.name.text(), bar.name.accessibleName())   # the whole sentence fits
        self.shot(bar, "stopped-fullscreen", "v7")
        QTest.mouseClick(bar.controls[0]["pause"], Qt.LeftButton)   # play resumes; the bar stays
        self.wait_for(lambda: daemon.controls == ["resume"] and not bar.control_busy)
        self.assertTrue(bar.isVisible())

    def test_no_window_is_stopped(self):
        """An older daemon's "no_window" shows the stopped view; its footage stays saveable."""
        daemon = FakeDaemon(True, extra={"state": "no_window", "recording": False, "target": "window",
                                         "error": "The game window closed"})
        bar = self.make(daemon)
        self.assert_stopped(bar, "window")
        self.assertTrue(bar.options[0].isEnabled())
        self.assertTrue(bar.hintbar.isHidden())
        bar.resident = True
        QTest.mouseClick(bar.controls[0]["pause"], Qt.LeftButton)
        self.wait_for(lambda: daemon.controls == ["resume"])
        self.wait_for(lambda: not bar.isVisible(), timeout=2)

    def test_stopped_keep_history_saves(self):
        daemon = FakeDaemon(True, extra={**self.STOPPED, "target": "window", "keep_history": True,
                                         "buffered": 45.0})
        bar = self.make(daemon)
        self.assert_stopped(bar, "window")
        # the same rule as while recording: every length is enabled, those past the footage dimmed
        self.assertTrue(all(o.isEnabled() for o in bar.options))
        self.assertEqual([o.property("long") for o in bar.options], [False, False] + [True] * 6)
        self.assertTrue(next(o for o in bar.options if o.seconds == overlay._last_choice()).hasFocus())
        self.shot(bar, "stopped-window-kept", "v7")
        self.key(Qt.Key_2)                                           # the last 30 s
        self.wait_done(bar)
        self.assertEqual(daemon.saves, [30])
        # without keep_history the lengths stay off, even if footage were reported
        bar2 = self.make(FakeDaemon(True, extra={**self.STOPPED, "buffered": 45.0}))
        self.assertTrue(all(not o.isEnabled() for o in bar2.options))
        # kept history, but nothing recorded yet
        bar3 = self.make(FakeDaemon(True, extra={**self.STOPPED, "keep_history": True, "buffered": 0}))
        self.assertTrue(all(not o.isEnabled() for o in bar3.options))

    def test_stop_confirm_keep_history(self):
        daemon = FakeDaemon(True, extra={"keep_history": True})
        bar = self.make(daemon)
        bar.ask_stop()
        self.assertEqual(bar.confirm.text(), "Stop recording?")
        pump(self.app, 0.05)
        self.shot(bar, "stop-confirm-kept", "v7")
        bar.cancel_confirm()
        daemon.extra["keep_history"] = False
        bar.apply_status(daemon.request({"cmd": "status"}))
        bar.ask_stop()
        self.assertEqual(bar.confirm.text(), "Stop and clear replay?")
        pump(self.app, 0.05)
        self.shot(bar, "stop-confirm-clear", "v7")
        # with the history kept a stop leaves the footage saveable
        daemon.extra["keep_history"] = True
        bar.cancel_confirm()
        bar.apply_status(daemon.request({"cmd": "status"}))
        bar.ask_stop()
        bar.confirm_stop()
        self.wait_for(lambda: daemon.controls == ["stop"] and not bar.control_busy)
        self.assert_stopped(bar)
        self.assertEqual(bar.buffered, 754.0)
        self.assertTrue(all(o.isEnabled() for o in bar.options))

    # ---------------------------------------------------------------- screenshot

    SHOT_BUTTONS = os.environ.get("MOMENTO_SHOT_BUTTONS_DIR")  # optional: the button-row pictures

    def shot_buttons(self, bar, name):
        self.shot(bar, name, "screenshot")
        if self.SHOT_BUTTONS:
            Path(self.SHOT_BUTTONS).mkdir(parents=True, exist_ok=True)
            bar.settle()
            img = bar.grab()
            canvas = QPixmap(img.width() + 80, img.height() + 60)
            canvas.fill(QColor("#4a5563"))
            p = QPainter(canvas)
            p.drawPixmap(QPoint(40, 20), img)
            p.end()
            canvas.save(str(Path(self.SHOT_BUTTONS) / f"bar-{name}.png"))

    def test_control_order_and_screenshot_enabled_only_while_recording(self):
        bar = self.make(FakeDaemon(True))
        for c in bar.controls:
            xs = [c[k].mapTo(bar, QPoint(0, 0)).x() for k in ("pause", "stop", "shot", "gear")]
            self.assertEqual(xs, sorted(xs))                     # play/pause, stop, screenshot, settings
        self.assertEqual(list(bar.controls[0]), ["pause", "stop", "shot", "gear"])
        shot = bar.controls[0]["shot"]
        self.assertEqual(shot.kind, "shot")
        self.assertTrue(shot.isEnabled())
        self.assertEqual(shot.accessibleName(), "Take a screenshot")
        shot.setFocus()
        pump(self.app, 0.05)
        self.shot_buttons(bar, "recording-screenshot-focus")
        bar.options[2].setFocus()
        pump(self.app, 0.05)
        self.shot_buttons(bar, "recording")
        paused = self.make(FakeDaemon(True, paused=True))
        self.assertFalse(paused.controls[0]["shot"].isEnabled())
        self.assertEqual(paused.controls[0]["shot"].visual_state, "disabled")
        self.shot_buttons(paused, "paused")
        # A disabled screenshot button is skipped by the keyboard: stop -> gear.
        paused.controls[0]["stop"].setFocus()
        self.key(Qt.Key_Right)
        self.assertTrue(paused.gear.hasFocus())
        paused.controls[0]["shot"].click()                   # and ignored if clicked anyway
        pump(self.app, 0.3)
        self.assertTrue(paused.isVisible())
        stopped = FakeDaemon(True)
        stopped.stopped = True
        off = self.make(stopped)
        self.assertEqual(off.view, "stopped")
        self.assertFalse(off.controls[0]["shot"].isEnabled())
        self.shot_buttons(off, "stopped")
        daemon_off = self.make(FakeDaemon(False))
        self.assertFalse(daemon_off.controls[0]["shot"].isEnabled())
        starting = self.make(FakeDaemon(True, extra={"state": "starting", "recording": False}))
        self.assertFalse(starting.controls[0]["shot"].isEnabled())
        # the recorded window closed (stopped; an older daemon's "no_window" shows the same)
        for extra in ({**self.STOPPED, "target": "window"},
                      {"state": "no_window", "recording": False, "target": "window"}):
            nowin = self.make(FakeDaemon(True, extra=extra))
            self.assertEqual(nowin.view, "stopped")
            self.assertFalse(nowin.controls[0]["shot"].isEnabled())

    def check_screenshot_hides_first(self, bar, daemon, activate):
        events = []
        daemon.on_shot = lambda: bar.isVisible()
        orig_hide = bar.hideEvent

        def hide_event(ev):
            events.append(("hidden", time.monotonic()))
            orig_hide(ev)
        bar.hideEvent = hide_event
        activate()
        self.wait_for(lambda: daemon.shots, timeout=3)
        (t_req, visible), = daemon.shots
        self.assertFalse(visible)                              # the bar was gone when the request went out
        self.assertEqual([e for e, _ in events], ["hidden"])
        self.assertGreaterEqual(t_req - events[0][1], overlay.SHOT_DELAY_MS / 1000 - 0.01)
        pump(self.app, 0.2)
        self.assertFalse(bar.isVisible())                      # no feedback in the bar: the notification says it
        self.assertEqual((daemon.saves, daemon.controls), ([], []))

    def test_screenshot_hides_bar_then_asks(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.resident = True
        shot = bar.controls[0]["shot"]
        self.check_screenshot_hides_first(bar, daemon, lambda: QTest.mouseClick(shot, Qt.LeftButton))
        # The next open starts afresh, the screenshot button ready again.
        bar.present()
        pump(self.app, 0.1)
        self.assertTrue(bar.isVisible())
        self.assertFalse(bar.done)
        self.assertTrue(bar.controls[0]["shot"].isEnabled())

    def test_screenshot_from_keyboard(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        bar.resident = True

        def keys():
            bar.options[-1].setFocus()
            for _ in range(3):
                self.key(Qt.Key_Right)                         # 60m -> pause, stop, screenshot
            self.assertTrue(bar.controls[0]["shot"].hasFocus())
            self.key(Qt.Key_Return)
        self.check_screenshot_hides_first(bar, daemon, keys)

    def test_screenshot_one_shot_bar_quits_after_reply(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.assertFalse(bar.resident)
        quits = []
        orig = QApplication.quit
        QApplication.quit = staticmethod(lambda: quits.append(True))
        self.addCleanup(setattr, QApplication, "quit", orig)
        self.addCleanup(self.app.setQuitOnLastWindowClosed, self.app.quitOnLastWindowClosed())
        self.check_screenshot_hides_first(bar, daemon, bar.controls[0]["shot"].click)
        self.wait_for(lambda: quits)

class AutoHide(unittest.TestCase):
    """The bar hides 3 s after the last input, and 0.5 s after the pointer leaves it."""

    make = OverlayOffscreen.make
    wait_for = OverlayOffscreen.wait_for
    key = OverlayOffscreen.key

    @classmethod
    def setUpClass(cls):
        OverlayOffscreen.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        OverlayOffscreen.tearDownClass.__func__(cls)

    def resident(self, daemon=None):
        bar = self.make(daemon or FakeDaemon(True))
        bar.resident = True                      # hiding is observable (a one-shot bar would quit)
        bar.idle.start()                         # as present() does
        return bar

    def leave(self, bar):
        self.app.sendEvent(bar, QEvent(QEvent.Leave))

    def test_untouched_hides_after_3_s_in_clip_and_settings(self):
        bar = self.resident()
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)
        self.key(Qt.Key_S)
        self.wait_for(lambda: bar.mode == "settings")
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)   # no longer 30 s in settings
        self.assertTrue(bar.idle.isActive())
        bar.idle.setInterval(300)                                     # (shortened for the test)
        bar.idle.start()
        self.wait_for(lambda: not bar.isVisible(), timeout=2)

    def test_any_input_restarts_the_3_s(self):
        bar = self.resident()
        for send in (lambda: self.key(Qt.Key_Right),
                     lambda: bar.on_pad_button("tl2", True),              # a controller button
                     lambda: self.app.sendEvent(bar.options[0], QMouseEvent(
                         QEvent.MouseMove, QPointF(5, 5), QPointF(5, 5), Qt.NoButton, Qt.NoButton,
                         Qt.NoModifier)),
                     lambda: self.app.sendEvent(bar, QWheelEvent(
                         QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                         Qt.NoModifier, Qt.NoScrollPhase, False))):
            bar.idle.stop()
            send()
            self.assertTrue(bar.idle.isActive())
            self.assertGreater(bar.idle.remainingTime(), overlay.IDLE_HIDE_MS - 200)
        self.assertTrue(bar.isVisible())

    def test_pointer_leaving_hides_after_half_a_second(self):
        bar = self.resident()
        self.leave(bar)
        self.assertTrue(bar.leave.isActive())
        self.assertEqual(bar.leave.interval(), overlay.LEAVE_HIDE_MS)
        pump(self.app, 0.2)
        self.app.sendEvent(bar.options[0], QEvent(QEvent.Enter))      # back over the bar: stays
        self.assertFalse(bar.leave.isActive())
        pump(self.app, 0.5)
        self.assertTrue(bar.isVisible())
        self.leave(bar)
        self.wait_for(lambda: not bar.isVisible(), timeout=2)
        self.assertLess(bar.leave.interval(), 1000)

    def test_leaving_during_a_save_waits_for_the_result(self):
        daemon = FakeDaemon(True)
        bar = self.resident(daemon)
        self.key(Qt.Key_Return)                                       # save (0.2 s at the fake daemon)
        self.assertTrue(bar.saving)
        self.leave(bar)
        pump(self.app, overlay.LEAVE_HIDE_MS / 1000 + 0.1)
        self.assertTrue(bar.isVisible())                              # the save goes on...
        self.wait_for(lambda: bar.done)
        self.assertIn("Saved", bar.line.text())                       # ...and its result shows
        self.wait_for(lambda: not bar.isVisible(), timeout=overlay.RESULT_CLOSE_MS / 1000 + 2)

    def test_leave_while_hidden_or_shown_again(self):
        bar = self.resident()
        self.leave(bar)
        bar.dismiss()
        self.assertFalse(bar.leave.isActive())
        bar.present()
        self.assertFalse(bar.leave.isActive())                        # a new open starts clean


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
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)
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


class ControllerBar(unittest.TestCase):
    """The bar driven by a game controller (FakeDevice pads; no real device is touched)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        cls._orig = ipc.request
        cls._tmp = tempfile.TemporaryDirectory()
        cls._last = overlay.LAST_FILE
        overlay.LAST_FILE = Path(cls._tmp.name) / "overlay.last"

    @classmethod
    def tearDownClass(cls):
        ipc.request = cls._orig
        overlay.LAST_FILE = cls._last
        cls._tmp.cleanup()

    make = OverlayOffscreen.make
    shot = OverlayOffscreen.shot
    wait_for = OverlayOffscreen.wait_for
    STOPPED = OverlayOffscreen.STOPPED

    def setUp(self):
        self.devs, self.hubs, self.made = [], [], []
        self.grab_error = None
        self.held = set()
        self.hat_y = 0
        self.addCleanup(setattr, overlay, "PAD_FACTORY", overlay.PAD_FACTORY)
        overlay.PAD_FACTORY = self.factory
        self.cfg = config.default_path()
        self.assertTrue(str(self.cfg).startswith(os.environ["MOMENTO_TEST_SANDBOX"]))
        self.addCleanup(self.cfg.unlink, True)

    def tearDown(self):
        for d in self.devs:
            d.close()

    def factory(self, **kw):
        """A hub over one fake pad (a fresh one per open, like re-opening /dev/input)."""
        self.made.append(kw)
        dev = gamepad.FakeDevice(name="Test pad", path=f"/fake/pad{len(self.devs)}", grab_error=self.grab_error)
        dev.held |= self.held
        dev.axes[gamepad.ABS_HAT0Y].value = self.hat_y
        self.devs.append(dev)
        hub = gamepad.Gamepads(lister=lambda: [dev.path], opener=lambda _p: dev, hotplug="off",
                               watchdog_thread=False, **kw)
        self.hubs.append(hub)
        return hub

    @property
    def dev(self):
        return self.devs[-1]

    def press(self, *codes, hold=0.0):
        for c in codes:
            self.dev.push(gamepad.EV_KEY, c, 1)
        pump(self.app, max(0.03, hold))
        for c in codes:
            self.dev.push(gamepad.EV_KEY, c, 0)
        pump(self.app, 0.03)

    def dpad(self, axis, value):
        self.dev.push(gamepad.EV_ABS, axis, value)
        self.dev.push(gamepad.EV_ABS, axis, 0)
        pump(self.app, 0.03)

    def right(self):
        self.dpad(gamepad.ABS_HAT0X, 1)

    def left(self):
        self.dpad(gamepad.ABS_HAT0X, -1)

    def down(self):
        self.dpad(gamepad.ABS_HAT0Y, 1)

    def up(self):
        self.dpad(gamepad.ABS_HAT0Y, -1)

    A, B, X, Y = gamepad.BTN_SOUTH, gamepad.BTN_EAST, gamepad.BTN_X, gamepad.BTN_Y   # xbox layout
    LB, RB = gamepad.BTN_TL, gamepad.BTN_TR

    def open(self, daemon):
        bar = self.make(daemon)
        self.wait_for(lambda: bar.pads is not None)
        return bar

    # ------------------------------------------------------------------ tests
    def test_grab_on_show_release_on_hide(self):
        bar = self.open(FakeDaemon(True))
        self.assertTrue(self.dev.grabbed)
        self.assertEqual(bar.pads.grab_state(), "exclusive")
        self.assertEqual(self.made[-1]["chord"], ("mode", "dpad_down"))
        self.assertEqual(self.made[-1]["hold_ms"], 0)            # the default: a tap
        self.assertTrue(self.made[-1]["navigate"])
        first = self.dev
        bar.hide()
        self.assertIsNone(bar.pads)
        self.assertFalse(first.grabbed)
        self.assertTrue(first.closed)                  # a hidden bar holds no fds
        bar.show()
        self.wait_for(lambda: bar.pads is not None)
        self.assertIsNot(self.dev, first)
        self.assertTrue(self.dev.grabbed)

    def test_resident_present_and_dismiss(self):
        bar = self.make(FakeDaemon(True))
        bar.hide()
        bar.resident = True
        bar.present()
        self.wait_for(lambda: bar.pads is not None)
        self.assertTrue(self.dev.grabbed)
        bar.dismiss()
        self.assertFalse(self.dev.grabbed)
        self.assertIsNone(bar.pads)

    def test_config_off_and_shared(self):
        config.set_value("controller", "exclusive", False, self.cfg)
        bar = self.open(FakeDaemon(True))
        self.assertFalse(self.dev.grabbed)
        self.assertEqual(bar.pads.grab_state(), "off")
        self.right()                                    # still navigates, just shared
        self.assertTrue(bar.options[overlay.PRESETS.index(next(p for p in overlay.PRESETS
                        if p[0] == overlay._last_choice())) + 1].hasFocus())
        bar.hide()
        config.set_value("controller", "enabled", False, self.cfg)
        n = len(self.made)
        bar.show()
        pump(self.app, 0.1)
        self.assertIsNone(bar.pads)
        self.assertEqual(len(self.made), n)            # not even opened

    def test_grab_failure_keeps_working(self):
        self.grab_error = 16                           # EBUSY: someone else holds it
        bar = self.open(FakeDaemon(True))
        self.assertFalse(self.dev.grabbed)
        self.assertEqual(bar.pads.grab_state(), "shared")
        self.press(self.Y)
        self.wait_for(lambda: bar.mode == "settings")

    def use_view_menu(self):
        config.set_value("controller", "open_chord", ["select", "start"], self.cfg)

    def test_held_chord_grabbed_after_release(self):
        """Opened by the chord: the pad is taken over only once it is released, so the
        game sees the release and nothing stays pressed in it."""
        self.use_view_menu()
        self.held = {gamepad.BTN_SELECT, gamepad.BTN_START}
        bar = self.open(FakeDaemon(True))
        self.assertFalse(self.dev.grabbed)
        self.dev.push(gamepad.EV_KEY, gamepad.BTN_SELECT, 0)
        self.dev.push(gamepad.EV_KEY, gamepad.BTN_START, 0)
        self.wait_for(lambda: self.dev.grabbed)
        self.assertTrue(bar.isVisible())               # the release didn't re-toggle it

    def test_default_chord_held_at_open(self):
        """Opened by PS + Down, still held: no focus move, no second toggle; the pad is
        taken once both are let go."""
        self.held, self.hat_y = {gamepad.BTN_MODE}, 1
        bar = self.open(FakeDaemon(True))
        focus = QApplication.focusWidget()
        self.assertFalse(self.dev.grabbed)
        self.dev.push(gamepad.EV_ABS, gamepad.ABS_HAT0Y, 0)
        self.dev.push(gamepad.EV_KEY, gamepad.BTN_MODE, 0)
        self.wait_for(lambda: self.dev.grabbed)
        self.assertTrue(bar.isVisible())
        self.assertIs(QApplication.focusWidget(), focus)

    def test_takes_the_pad_from_the_daemon(self):
        """The daemon still holds the pad for PS + Down (EBUSY, and the bar sees none of
        its events): the bar keeps trying and takes it once PS is let go."""
        self.grab_error = 16
        self.held = {gamepad.BTN_MODE}
        bar = self.open(FakeDaemon(True))
        self.dev.grabbed_by_other = True
        pump(self.app, 0.2)
        self.assertFalse(self.dev.grabbed)
        self.dev.push(gamepad.EV_KEY, gamepad.BTN_MODE, 0)          # unseen by the bar
        self.dev.grab_error, self.dev.grabbed_by_other = None, False  # the daemon lets go
        self.wait_for(lambda: self.dev.grabbed, timeout=2)
        self.assertEqual(bar.pads.grab_state(), "exclusive")
        bar.resident = True
        self.down()                                     # a plain Down navigates; it doesn't close
        self.assertTrue(bar.isVisible())

    def test_clip_view(self):
        overlay.LAST_FILE.unlink(missing_ok=True)      # default length: 1m
        daemon = FakeDaemon(True)
        bar = self.open(daemon)
        i = next(i for i, o in enumerate(bar.options) if o.hasFocus())
        self.right()
        self.assertTrue(bar.options[i + 1].hasFocus())
        self.left()
        self.left()
        self.assertTrue(bar.options[i - 1].hasFocus())
        self.down()                                    # down = next, like the keyboard
        self.assertTrue(bar.options[i].hasFocus())
        self.press(self.RB)                            # bumpers jump between groups
        self.assertTrue(bar.controls[0]["pause"].hasFocus())   # the first button
        self.press(self.LB)
        self.assertTrue(bar.options[i].hasFocus())
        self.press(self.LB)
        self.assertTrue(bar.options[0].hasFocus())
        self.assertTrue(bar.focus_visible)
        bar.options[i].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "clip-controller-focus", "v6")
        self.press(self.A)                             # save
        self.wait_for(lambda: daemon.saves == [bar.options[i].seconds])
        self.wait_for(lambda: bar.done)

    def test_screenshot_with_controller(self):
        daemon = FakeDaemon(True)
        bar = self.open(daemon)
        bar.resident = True
        daemon.on_shot = lambda: bar.isVisible()
        self.press(self.RB)                            # the buttons: play/pause first
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.right()
        self.right()                                   # stop, screenshot
        self.assertTrue(bar.controls[0]["shot"].hasFocus())
        self.press(self.A)
        self.wait_for(lambda: daemon.shots)
        self.assertEqual(daemon.shots[0][1], False)    # hidden before the request
        self.assertFalse(bar.isVisible())
        self.assertFalse(self.dev.grabbed)             # a hidden bar lets go of the controller

    def test_back_closes_and_x_pauses(self):
        daemon = FakeDaemon(True)
        bar = self.open(daemon)
        self.press(self.X)
        self.wait_for(lambda: daemon.controls == ["pause"])
        self.wait_for(lambda: bar.view == "paused")
        bar.resident = True
        self.press(self.B)
        self.assertFalse(bar.isVisible())
        self.assertFalse(self.devs[-1].grabbed)

    def test_settings_view(self):
        daemon = FakeDaemon(True)
        bar = self.open(daemon)
        self.press(self.Y)
        self.wait_for(lambda: bar.mode == "settings")
        pump(self.app, 0.05)
        self.assertEqual(bar.tab_names[bar.tab], "General")
        self.assertTrue(bar.row("record").buttons[1].hasFocus())   # Window
        self.press(self.RB)                            # bumpers switch tabs: Video
        self.assertEqual(bar.tab_names[bar.tab], "Video")
        self.assertTrue(bar.row("resolution").buttons[1].hasFocus())
        self.right()                                   # Native
        self.assertEqual(bar.row("resolution").value, "native")
        self.left()
        self.press(self.LB)                            # back to General
        self.assertTrue(bar.row("record").buttons[1].hasFocus())
        self.left()                                    # Full screen
        self.assertEqual(bar.row("record").value, "screen")
        self.right()
        self.up()                                      # the tab row
        self.assertTrue(bar.tab_btns[0].hasFocus())
        self.right()                                   # D-pad on the tabs: Video
        self.press(self.RB)                            # a bumper on the tabs stays on the tabs
        self.assertEqual((bar.tab_names[bar.tab], bar.tab_btns[2].hasFocus()), ("Audio", True))
        self.right()                                   # Controller
        self.press(self.A)                             # into the tab
        ctl = bar.row("controller")
        self.assertTrue(ctl.buttons[1].hasFocus())
        self.assertEqual([b.text() for b in ctl.buttons], ["Off", "PS / Xbox + Down"])
        self.assertEqual(ctl.icon.kind, "gamepad")
        self.right()                                   # nothing past PS / Xbox + Down
        self.assertEqual(ctl.value, "ps_down")
        self.left()                                    # Off
        self.assertEqual(ctl.value, "off")
        self.assertEqual(bar.changes(), {"controller": "off"})
        self.down()                                    # the footer (the tab's only row)
        self.assertTrue(bar.apply_btn.hasFocus())
        self.right()
        self.assertTrue(bar.back_btn.hasFocus())
        self.left()
        pump(self.app, 0.05)
        self.shot(bar, "settings-controller", "v6")
        self.press(self.A)                             # apply
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(daemon.configures, [{"controller": "off"}])
        self.assertIn("controller updated", bar.foot.text())
        self.shot(bar, "settings-controller-saved", "v6")
        self.assertNotEqual((bar.last_status or {}).get("state"), "starting")   # nothing restarted

    def test_settings_resolution_skips_what_the_screen_cannot_show(self):
        daemon = FakeDaemon(True, source=[1280, 720], values={"record": "screen", "resolution": "720p"})
        bar = self.open(daemon)
        self.press(self.Y)
        self.wait_for(lambda: bar.mode == "settings")
        pump(self.app, 0.05)
        self.press(self.RB)                            # Video
        res = bar.row("resolution")
        self.assertTrue(res.buttons[0].hasFocus())     # 720p
        self.right()                                   # 1080p is skipped
        self.assertEqual(res.value, "native")
        self.assertTrue(res.buttons[2].hasFocus())
        self.left()
        self.assertEqual(res.value, "720p")
        self.assertEqual(res.note.text(), "Your screen is 720p")

    def test_settings_back(self):
        daemon = FakeDaemon(True, values={"record": "window"}, extra={"target": "window"})
        bar = self.open(daemon)
        self.press(self.Y)
        self.wait_for(lambda: bar.mode == "settings")
        pump(self.app, 0.05)
        self.right()                                   # nothing past Window (no Change window)
        self.assertTrue(bar.row("record").buttons[1].hasFocus())
        self.press(self.B)                             # Back
        self.assertEqual(bar.mode, "clip")
        self.assertEqual((daemon.controls, daemon.configures), ([], []))

    def test_stop_confirmation(self):
        daemon = FakeDaemon(True)
        bar = self.open(daemon)
        self.press(self.RB)                            # pause (the first button)
        self.right()                                   # stop
        self.assertTrue(bar.controls[0]["stop"].hasFocus())
        self.press(self.A)
        self.assertEqual(bar.mode, "confirm")
        self.assertTrue(bar.stop_no.hasFocus())        # the safe choice first
        self.right()
        self.assertTrue(bar.stop_yes.hasFocus())
        self.press(self.LB)
        self.assertTrue(bar.stop_no.hasFocus())
        self.press(self.B)                             # cancel
        self.assertEqual(bar.mode, "clip")
        self.assertEqual(daemon.controls, [])
        self.press(self.A)                             # stop again (focus is back on it)
        self.left()
        self.press(self.A)                             # Stop
        self.wait_for(lambda: daemon.controls == ["stop"])

    def test_stopped_and_off(self):
        daemon = FakeDaemon(True, extra={**self.STOPPED, "target": "window"})
        bar = self.open(daemon)
        self.assertEqual(bar.view, "stopped")
        bar.resident = True
        self.press(self.X)                             # play: the daemon opens the window picker
        self.wait_for(lambda: daemon.controls == ["resume"])
        self.wait_for(lambda: not bar.isVisible(), timeout=2)
        self.assertFalse(self.dev.grabbed)

        calls = []
        orig = overlay.start_daemon
        overlay.start_daemon = lambda: (calls.append(1), setattr(off, "running", True))
        self.addCleanup(setattr, overlay, "start_daemon", orig)
        off = FakeDaemon(False)
        bar = self.open(off)
        self.assertEqual(bar.view, "off")
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        self.press(self.A)                             # play starts Momento
        self.wait_for(lambda: calls == [1])

    def test_chord_closes_on_a_tap_by_default(self):
        """PS + Down (the default) on a tap closes the bar, without moving the focus first."""
        bar = self.open(FakeDaemon(True))
        bar.resident = True
        focus = QApplication.focusWidget()
        self.dev.push(gamepad.EV_KEY, gamepad.BTN_MODE, 1)
        pump(self.app, 0.03)
        self.assertIs(QApplication.focusWidget(), focus)
        self.dev.push(gamepad.EV_ABS, gamepad.ABS_HAT0Y, 1)       # a quick press is enough
        pump(self.app, 0.03)
        self.assertFalse(bar.isVisible())
        self.assertFalse(self.dev.grabbed)

    def test_view_menu_still_closes(self):
        self.use_view_menu()
        bar = self.open(FakeDaemon(True))
        bar.resident = True
        self.press(gamepad.BTN_SELECT, gamepad.BTN_START)
        self.assertFalse(bar.isVisible())

    def test_chord_closes_when_held(self):
        self.use_view_menu()
        config.set_value("controller", "hold_ms", 300, self.cfg)   # hand-edited (config only)
        bar = self.open(FakeDaemon(True))
        bar.resident = True
        self.press(gamepad.BTN_SELECT, gamepad.BTN_START, hold=0.2)
        self.assertTrue(bar.isVisible())               # too short
        self.press(gamepad.BTN_SELECT, gamepad.BTN_START, hold=0.7)
        self.assertFalse(bar.isVisible())
        self.assertFalse(self.dev.grabbed)

    def test_chord_left_to_daemon_when_shared(self):
        """A shared pad's chord also reaches the daemon, which toggles the bar: the bar
        must not close it as well (that would close and reopen it)."""
        self.grab_error = 16
        self.use_view_menu()
        bar = self.open(FakeDaemon(True))
        bar.resident = True
        self.press(gamepad.BTN_SELECT, gamepad.BTN_START, hold=0.7)
        self.assertTrue(bar.isVisible())

    def test_custom_chord_and_hold_from_config(self):
        config.set_value("controller", "open_chord", ["left_paddle"], self.cfg)
        config.set_value("controller", "hold_ms", 100, self.cfg)
        bar = self.open(FakeDaemon(True))
        self.assertEqual((self.made[-1]["chord"], self.made[-1]["hold_ms"]), (("left_paddle",), 100))
        bar.resident = True
        self.press(gamepad.BTN_TRIGGER_HAPPY1 + 6, hold=0.3)   # paddle 3 (upper left)
        self.assertFalse(bar.isVisible())

    def test_settings_row_custom_and_unavailable(self):
        daemon = FakeDaemon(True, values={"controller": "select+mode"})
        bar = self.open(daemon)
        self.press(self.Y)
        self.wait_for(lambda: bar.mode == "settings")
        ctl = bar.row("controller")
        self.assertEqual((ctl.value, ctl.labels[-1]), ("select+mode", "View + Xbox"))   # the pad's names
        self.press(self.B)
        orig = gamepad.available
        gamepad.available = lambda: False                # no python-evdev: no row
        self.addCleanup(setattr, gamepad, "available", orig)
        bar.sdata = settings_reply()
        bar.sdata["controller_available"] = False
        bar.build_rows()
        self.assertNotIn("controller", [r.key for r in bar.rows])
        bar.clear_rows()


class DrmScreenSize(unittest.TestCase):
    """The Resolution cap's fallback reads native modes from DRM, never Qt's rounded scale."""

    def make(self, root, name, status="connected", enabled="enabled", modes="1920x1080\n1280x720\n"):
        d = Path(root) / name
        d.mkdir()
        (d / "status").write_text(status + "\n")
        (d / "enabled").write_text(enabled + "\n")
        (d / "modes").write_text(modes)

    def test_largest_enabled_native_mode(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(overlay.drm_screen_size(root))                      # nothing connected
            self.make(root, "card1-eDP-1", enabled="disabled", modes="2560x1600\n")  # screen off
            self.make(root, "card1-DP-2")                                        # 1080p, any scaling
            self.make(root, "card1-HDMI-A-1", status="disconnected", modes="")
            self.make(root, "card1-DP-3", modes="garbage\n")
            self.assertEqual(overlay.drm_screen_size(root), (1920, 1080))
            self.make(root, "card1-DP-4", modes="3440x1440\n1920x1080\n")
            self.assertEqual(overlay.drm_screen_size(root), (3440, 1440))
        self.assertIsNone(overlay.drm_screen_size("/nonexistent/drm"))


if __name__ == "__main__":
    unittest.main()

