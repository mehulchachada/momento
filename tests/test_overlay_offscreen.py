"""Render the replay bar offscreen with a stubbed daemon and save screenshots.

    python3 -m unittest tests.test_overlay_offscreen

Screenshots land in /tmp/claude-1000/momento-{overlay,settings,controls}-*.png
(override with $MOMENTO_SHOT_DIR). Each is the bar composited over a plain backdrop that
stands in for the game.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_WAYLAND_SHELL_INTEGRATION", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from momento import config, ipc, overlay, settings  # noqa: E402

SHOT_DIR = Path(os.environ.get("MOMENTO_SHOT_DIR", "/tmp/claude-1000"))

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


def settings_reply(devices=DEVICES, **values):
    cfg = config.load(Path("/nonexistent/momento-test.toml"))
    for k, v in values.items():
        for section, key, val in settings.writes(k, settings.normalize(k, v)):
            cfg[section][key] = val
    return settings.describe(cfg, devices=devices)


class FakeDaemon:
    def __init__(self, running=True, fail=False, devices=DEVICES, paused=False):
        self.running, self.fail, self.devices, self.paused = running, fail, devices, paused
        self.saves = []
        self.configures = []
        self.controls = []

    def request(self, msg, timeout=120, **_):
        if not self.running:
            raise ipc.DaemonNotRunning("no socket")
        if msg["cmd"] == "status":
            st = dict(STATUS)
            if self.paused:
                st.update(state="paused", recording=False)
            return st
        if msg["cmd"] == "settings":
            return settings_reply(self.devices)
        if msg["cmd"] == "configure":
            self.configures.append(msg["changes"])
            time.sleep(0.3)
            return {"ok": True, "changed": msg["changes"], "restarted": True, "paused": False}
        if msg["cmd"] in ("pause", "resume", "quit"):
            self.controls.append(msg["cmd"])
            if msg["cmd"] == "pause":
                self.paused = True
            elif msg["cmd"] == "resume":
                self.paused = False
            elif msg["cmd"] == "quit":
                self.running = False
            return {"ok": True, "state": "paused" if self.paused else "starting"}
        if msg["cmd"] == "save":
            self.saves.append(msg["seconds"])
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
        self.shot(bar, "normal")

        self.key(Qt.Key_Return)  # saves 5m
        pump(self.app, 0.05)
        self.assertTrue(bar.saving)
        self.assertEqual(bar.stack.currentIndex(), 1)
        self.assertIn("Saving last 5m", bar.line.text())
        self.shot(bar, "saving")
        self.wait_done(bar)
        self.assertTrue(bar.done)
        self.assertEqual(daemon.saves, [300])
        self.assertIn("Replay_2026-09-26_21-04-11_5m.mp4", bar.line.text())
        self.assertEqual(overlay._last_choice(), 300)
        self.shot(bar, "saved")

    def test_number_key_and_error(self):
        daemon = FakeDaemon(True, fail=True)
        bar = self.make(daemon)
        self.key(Qt.Key_1)
        self.wait_done(bar)
        self.assertEqual(daemon.saves, [15])
        self.assertIn("encoder stalled", bar.line.text())
        self.shot(bar, "error")

    def test_daemon_off(self):
        daemon = FakeDaemon(False)
        bar = self.make(daemon)
        self.assertEqual(bar.stack.currentIndex(), 1)
        self.assertIn("Replay is off", bar.line.text())
        self.key(Qt.Key_3)  # ignored while off
        self.assertEqual(daemon.saves, [])
        self.shot(bar, "off")

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
        self.assertTrue(bar.row("resolution").buttons[1].hasFocus())  # 1080p
        self.assertEqual([b.text() for b in bar.row("resolution").buttons],
                         ["720p", "1080p", "1440p", "4K", "Native"])
        self.key(Qt.Key_Right)                   # 1440p
        self.assertIn("11 GB", bar.foot.text().replace("10.8", "11"))
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
        self.wait_for(lambda: bar.mode == "clip", timeout=overlay.RESULT_CLOSE_MS / 1000 + 2)
        self.assertEqual(bar.height(), h0)
        self.assertEqual(bar.y() + bar.height(), bottom0)

    def test_settings_click_esc_and_no_changes(self):
        daemon = FakeDaemon(True)
        bar = self.make(daemon)
        self.open_settings(bar)
        self.assertIn("Applying restarts the replay buffer", bar.note.text())
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
        self.key(Qt.Key_Down)
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
        self.assertFalse(bar.start_btn.isHidden())
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
        self.wait_for(lambda: bar.mode == "clip", timeout=overlay.RESULT_CLOSE_MS / 1000 + 2)
        self.assertIn("Replay is off", bar.line.text())

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
        self.assertIn("Resuming starts a fresh replay", bar.hintbar.text())
        self.assertEqual(bar.height(), h0 + overlay.HINT_H + 1)
        self.assertEqual(bar.stack.currentIndex(), 0)     # clips still saveable
        bar.controls[0]["pause"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "paused", "controls")
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
        self.assertIn("Stop Momento?", bar.confirm.text())
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
        self.key(Qt.Key_Return)
        self.wait_for(lambda: daemon.controls == ["quit"] and not bar.control_busy)
        bar.refresh_async()
        self.wait_for(lambda: "Replay is off" in bar.line.text())
        self.assertFalse(bar.start_btn.isHidden())
        self.assertTrue(bar.controls[1]["stop"].isHidden())
        self.shot(bar, "off-start", "controls")

    def test_start_from_off(self):
        daemon = FakeDaemon(False)
        calls = []

        def fake_start():
            calls.append(1)
            daemon.running = True
            return "process"
        orig = overlay.start_daemon
        overlay.start_daemon = fake_start
        self.addCleanup(setattr, overlay, "start_daemon", orig)
        bar = self.make(daemon)
        self.assertTrue(bar.start_btn.hasFocus())
        self.key(Qt.Key_Return)
        self.wait_for(lambda: bar.online is True, timeout=5)
        self.assertEqual(calls, [1])
        self.assertEqual(bar.stack.currentIndex(), 0)


if __name__ == "__main__":
    unittest.main()
