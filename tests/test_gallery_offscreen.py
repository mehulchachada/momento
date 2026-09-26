"""The clip bar's gallery, offscreen, against a fake daemon and a fake media player.

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gallery_offscreen

Screenshots of each state land in $MOMENTO_SHOT_DIR (default /tmp/claude-1000) as
momento-gallery-*.png. No real daemon, socket, config, controller or audio device
is touched: the clips folder is a temporary one in the test sandbox, the player is
``FakePlayer`` (through ``gallery.PLAYER_FACTORY``), and the pads are FakeDevices.
One live test plays a 2 s clip made with ffmpeg through the real QMediaPlayer
(skipped without ffmpeg or QtMultimedia), still muted, so no audio output is made.
"""

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401
import math
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_WAYLAND_SHELL_INTEGRATION", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QPoint, QPointF, QSize, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap, QPolygonF  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from momento import config, gamepad, ipc, overlay  # noqa: E402

# The module, not its classes: a TestCase imported by name would run here a second time.
try:
    from tests import test_overlay_offscreen as base  # noqa: E402
except ImportError:
    import test_overlay_offscreen as base  # noqa: E402
FakeDaemon, SHOT_DIR, pump = base.FakeDaemon, base.SHOT_DIR, base.pump

try:
    from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame
except ImportError:  # the gallery can't play without it; these tests need it
    QMediaPlayer = QVideoFrame = None


# --------------------------------------------------------------------------
# pictures (generated; nothing from a real game)
# --------------------------------------------------------------------------

def scene(kind, w=1920, h=1080):
    """A dusk landscape (clips) or a night one (screenshots)."""
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    sky = QLinearGradient(0, 0, 0, h * 0.75)
    stops = ((0, "#140C2B"), (0.45, "#43183F"), (0.8, "#A8392F"), (1, "#F08A3A")) if kind == "clip" else \
            ((0, "#050C1A"), (0.6, "#0D2536"), (1, "#173B46"))
    for at, col in stops:
        sky.setColorAt(at, QColor(col))
    p.fillRect(0, 0, w, h, sky)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FFE3C4" if kind == "clip" else "#DDEEFF"))
    p.drawEllipse(QPointF(w * 0.72, h * 0.3), h * 0.12, h * 0.12)
    for i, col in enumerate(("#7C2F3E", "#4E1B33", "#2A0F22", "#120713") if kind == "clip" else
                            ("#0F2430", "#0B1A23", "#070D12", "#030507")):
        base = h * (0.52 + 0.1 * i)
        path = QPainterPath(QPointF(0, h))
        for x in range(0, w + 40, 40):
            path.lineTo(x, base + 50 * math.sin(x * 0.004 + i * 1.7) + 20 * math.sin(x * 0.011 + i))
        path.lineTo(w, h)
        path.closeSubpath()
        p.setBrush(QColor(col))
        p.drawPath(path)
    p.setBrush(QColor(255, 255, 255, 220))
    p.drawPolygon(QPolygonF([QPointF(w * 0.33, h * 0.84), QPointF(w * 0.35, h * 0.7), QPointF(w * 0.37, h * 0.84)]))
    p.end()
    return img


CLIP_IMAGE = None


def clip_image():
    global CLIP_IMAGE
    if CLIP_IMAGE is None:
        CLIP_IMAGE = scene("clip")
    return CLIP_IMAGE


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeAudio(QObject):
    made = []

    def __init__(self, parent=None):
        super().__init__(parent)
        FakeAudio.made.append(self)


class FakePlayer(QObject):
    """QMediaPlayer's surface as the gallery uses it; records every call."""

    positionChanged = Signal(object)
    durationChanged = Signal(object)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)

    made = []
    duration_ms = 60_000
    fail = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []
        self.sink = self.audio = None
        self.pos = 0
        self.dur = 0
        self.src = ""
        self.state = QMediaPlayer.PlaybackState.StoppedState
        FakePlayer.made.append(self)

    # what the gallery calls
    def setVideoSink(self, sink):
        self.calls.append(("sink", sink))
        self.sink = sink

    def setAudioOutput(self, audio):
        self.calls.append(("audio", audio))
        self.audio = audio

    def setSource(self, url):
        path = url.toLocalFile()
        self.calls.append(("setSource", path))
        self.src, self.pos = path, 0
        if not path:
            return
        if FakePlayer.fail:
            self.errorOccurred.emit(QMediaPlayer.Error.FormatError, "unsupported")
            return
        self.dur = FakePlayer.duration_ms
        self.durationChanged.emit(self.dur)
        self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)

    def _set(self, state):
        if state != self.state:
            self.state = state
            self.playbackStateChanged.emit(state)

    def play(self):
        self.calls.append("play")
        if FakePlayer.fail or not self.src:
            return
        self._set(QMediaPlayer.PlaybackState.PlayingState)
        if self.sink is not None:
            self.sink.setVideoFrame(QVideoFrame(clip_image()))

    def pause(self):
        self.calls.append("pause")
        self._set(QMediaPlayer.PlaybackState.PausedState)

    def stop(self):
        self.calls.append("stop")
        self._set(QMediaPlayer.PlaybackState.StoppedState)

    def setPosition(self, ms):
        self.calls.append(("setPosition", ms))
        self.pos = ms
        self.positionChanged.emit(ms)

    def position(self):
        return self.pos

    def duration(self):
        return self.dur

    def playbackState(self):
        return self.state

    # what the test does
    def advance(self, ms):
        self.pos = min(self.dur, self.pos + ms)
        self.positionChanged.emit(self.pos)

    def end(self):
        self.pos = self.dur
        self.positionChanged.emit(self.pos)
        self._set(QMediaPlayer.PlaybackState.StoppedState)
        self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)

    def sources(self):
        return [c[1] for c in self.calls if isinstance(c, tuple) and c[0] == "setSource" and c[1]]


class FakeHub:
    """Stands in for the bar's controller hub (renew() bookkeeping only)."""

    def __init__(self):
        self.renewed = 0

    def renew(self, now=None):
        self.renewed += 1

    def close(self):
        pass


# --------------------------------------------------------------------------
# the tests
# --------------------------------------------------------------------------

@unittest.skipIf(QMediaPlayer is None, "PySide6.QtMultimedia is missing")
class GalleryOffscreen(unittest.TestCase):
    make = base.OverlayOffscreen.make
    wait_for = base.OverlayOffscreen.wait_for
    key = base.OverlayOffscreen.key

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        cls._orig = ipc.request
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ["MOMENTO_TEST_SANDBOX"])
        cls._last = overlay.LAST_FILE
        overlay.LAST_FILE = Path(cls._tmp.name) / "overlay.last"
        # A clips folder: newest first it reads clip0, shot0, clip1, shot1, clip2.
        cls.out = Path(cls._tmp.name) / "Momento"
        (cls.out / "Images").mkdir(parents=True)
        now = time.time()
        cls.now = now

        def put(name, mtime, data=b"\0" * 1024):
            path = cls.out / name
            path.write_bytes(data) if isinstance(data, bytes) else data.save(str(path))
            os.utime(path, (mtime, mtime))
            return path
        cls.clip0 = put("Replay_a_1m.mp4", now - 5)
        cls.shot0 = put("Images/Momento_b.png", now - 30, scene("shot"))
        cls.clip1 = put("Replay_c_30s.mp4", now - 60)
        cls.shot1 = put("Images/Momento_d.png", now - 7200, scene("clip", 1280, 720))
        cls.clip2 = put("Replay_e_5m.mp4", now - 26 * 3600)
        cls.empty = Path(cls._tmp.name) / "Empty"
        cls.empty.mkdir()

    @classmethod
    def tearDownClass(cls):
        ipc.request = cls._orig
        overlay.LAST_FILE = cls._last
        cls._tmp.cleanup()

    def setUp(self):
        from momento import gallery

        self.gallery_mod = gallery
        FakePlayer.made, FakeAudio.made = [], []
        FakePlayer.fail, FakePlayer.duration_ms = False, 60_000
        self.addCleanup(setattr, gallery, "PLAYER_FACTORY", gallery.PLAYER_FACTORY)
        self.addCleanup(setattr, gallery, "AUDIO_FACTORY", gallery.AUDIO_FACTORY)
        gallery.PLAYER_FACTORY = FakePlayer
        gallery.AUDIO_FACTORY = FakeAudio
        # clip lengths without ffprobe: the fake files are not real MP4s
        self.addCleanup(setattr, gallery.media, "clip_duration", gallery.media.clip_duration)
        gallery.media.clip_duration = lambda path, timeout=3.0: {"Replay_a_1m.mp4": 60.0,
                                                                  "Replay_c_30s.mp4": 30.0}.get(Path(path).name)

    # ------------------------------------------------------------ helpers
    def daemon(self, folder=None, **kw):
        return FakeDaemon(True, extra={"output_dir": str(folder or self.out)}, **kw)

    def bar(self, folder=None):
        bar = self.make(self.daemon(folder))
        bar.focus_visible = True
        self.addCleanup(bar.close_gallery)
        return bar

    def open(self, bar):
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        pump(self.app, 0.05)
        return bar.gallery

    @property
    def player(self):
        return FakePlayer.made[-1]

    def names(self, g):
        return [i.path.name for i in g.view]

    def settle_items(self, g):
        """Past the browse debounce and any image load."""
        pump(self.app, (self.gallery_mod.STEP_MS + 80) / 1000)
        self.wait_for(lambda: g.state != "loading" or not g.is_shot(), timeout=3)
        pump(self.app, 0.03)

    def full_shot(self, view, name):
        """A full screen view as it looks on a 1920x1080 screen (the offscreen one is smaller)."""
        view.showNormal()
        view.resize(1920, 1080)
        pump(self.app, 0.1)
        self.shot(view, name, margin=False)
        view.showFullScreen()
        pump(self.app, 0.05)

    def shot(self, widget, name, margin=True):
        """Save ``widget`` (the bar over a plain backdrop, or a full screen view as is)."""
        for b in widget.findChildren(QPushButton):
            if hasattr(b, "settle"):
                b.settle()                # pill transitions at their end state
        pump(self.app, 0.02)
        img = widget.grab()
        if not margin:
            img.save(str(SHOT_DIR / f"momento-gallery-{name}.png"))
            return
        canvas = QPixmap(img.width() + 80, img.height() + 40)
        canvas.fill(QColor("#4a5563"))
        p = QPainter(canvas)
        p.drawPixmap(QPoint(40, 20), img)
        p.end()
        canvas.save(str(SHOT_DIR / f"momento-gallery-{name}.png"))

    # ------------------------------------------------------------ the button
    def test_button_left_of_storage(self):
        bar = self.bar()
        btn, hint = bar.gallery_btn, bar.storage_hint
        self.assertIs(btn.parentWidget(), hint.parentWidget())       # in the head block
        self.assertEqual(btn.x() + btn.width() + 4, hint.x())         # directly left of the free space
        self.assertEqual(btn.kind, "gallery")
        self.assertEqual(btn.accessibleName(), "Gallery (G)")
        self.assertEqual(bar.width(), 1040)
        items = bar.focusables()
        self.assertIs(items[0], btn)                                  # first in the focus order
        bar.options[0].setFocus()
        self.key(Qt.Key_Left)                                         # left from the first length
        self.assertTrue(btn.hasFocus())
        self.key(Qt.Key_Right)
        self.assertTrue(bar.options[0].hasFocus())
        bar.options[2].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "01-bar")
        btn.setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "02-bar-button-focused")

    def test_bumpers_reach_the_button(self):
        bar = self.bar()
        overlay.LAST_FILE.unlink(missing_ok=True)
        bar.focus_default()
        i = next(i for i, o in enumerate(bar.options) if o.hasFocus())
        bar.on_pad_action("prev_section")
        self.assertTrue(bar.options[0].hasFocus())
        bar.on_pad_action("prev_section")
        self.assertTrue(bar.gallery_btn.hasFocus())
        bar.on_pad_action("prev_section")                             # nothing further left
        self.assertTrue(bar.gallery_btn.hasFocus())
        bar.on_pad_action("next_section")                             # back to the lengths
        self.assertTrue(bar.options[i].hasFocus())
        bar.on_pad_action("next_section")
        self.assertTrue(bar.controls[0]["pause"].hasFocus())
        bar.on_pad_action("prev_section")
        self.assertTrue(bar.options[i].hasFocus())

    # ------------------------------------------------------------ opening
    def test_open_by_click(self):
        bar = self.bar()
        h0 = bar.height()
        bar.move(100, 900)
        bottom = bar.y() + bar.height()
        QTest.mouseClick(bar.gallery_btn, Qt.LeftButton)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        self.assertEqual(bar.height(), h0 + g.panel_height() + 1)
        self.assertEqual(bar.height(), 721)                           # the mockup's panel
        self.assertEqual((g.stage.width(), g.stage.height()), (1006, 566))
        self.assertEqual(bar.y() + bar.height(), bottom)              # grew upward
        self.assertEqual(bar.stack.currentIndex(), 4)
        self.assertFalse(bar.gallery_host.isHidden())
        self.assertTrue(bar.panel.isHidden())

    def test_short_screen_shrinks_the_stage(self):
        """1280x720 (the Ally at 150 %): the stage gets smaller so the whole bar fits."""
        bar = self.bar()
        g = self.open(bar)
        self.key(Qt.Key_Escape)
        screen = bar.screen()
        orig = type(screen).availableGeometry
        from PySide6.QtCore import QRect
        self.addCleanup(setattr, type(screen), "availableGeometry", orig)
        type(screen).availableGeometry = lambda _s: QRect(0, 0, 1280, 720)
        g = self.open(bar)
        self.assertLessEqual(bar.height() + 2 * overlay.BOTTOM_MARGIN, 720)
        self.assertEqual(round(g.stage.width() * 9 / 16), g.stage.height())
        self.assertLess(g.stage.width(), 1006)
        self.assertEqual(bar.width(), 1040)                           # the bar itself keeps its width
        self.shot(bar, "12-short-screen")

    def test_open_by_controller(self):
        bar = self.bar()
        bar.gallery_btn.setFocus()
        bar.on_pad_action("accept")
        self.wait_for(lambda: bar.mode == "gallery")
        self.assertEqual(bar.gallery.current().path, self.clip0)

    def test_newest_first_muted_autoplay(self):
        bar = self.bar()
        g = self.open(bar)
        self.assertEqual(self.names(g), [p.name for p in (self.clip0, self.shot0, self.clip1, self.shot1, self.clip2)])
        self.assertEqual(g.index, 0)
        self.assertEqual(self.player.sources(), [str(self.clip0)])    # the newest, at once
        self.assertIn("play", self.player.calls)
        self.assertEqual(g.state, "playing")
        self.assertTrue(g.muted)
        self.assertNotIn("audio", [c[0] for c in self.player.calls if isinstance(c, tuple) and c[1] is not None])
        self.assertEqual(FakeAudio.made, [])                          # no audio output while muted
        self.assertIsNotNone(g.frame)
        self.assertEqual(g.panel.w["play"].kind, "pause")
        self.assertEqual(g.panel.w["mute"].kind, "muted")
        self.assertTrue(g.panel.w["play"].hasFocus())
        self.assertEqual(g.panel.w["counter"].text(), "1 / 5")
        self.assertTrue(g.tabs["all"].selected())
        self.assertIn("Clip", g.footer.meta.text())
        self.assertIn("1:00", g.footer.meta.text())
        self.assertIn("Today", g.footer.meta.text())
        self.assertEqual(g.footer.hint, self.gallery_mod.CLIP_HINT)
        self.player.advance(12_000)
        self.assertEqual(g.panel.w["now"].text(), "0:12")
        self.assertAlmostEqual(g.panel.w["scrub"].value, 0.2, places=3)
        self.shot(bar, "03-clip-playing")

    def test_paused_sound_on(self):
        bar = self.bar()
        g = self.open(bar)
        self.player.advance(37_000)
        self.key(Qt.Key_Space)                                        # pause
        self.assertEqual((g.state, g.panel.w["play"].kind), ("paused", "play"))
        self.assertIn("pause", self.player.calls)
        self.key(Qt.Key_M)                                            # sound on
        self.assertFalse(g.muted)
        g.panel.w["play"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "04-clip-paused-sound")
        self.key(Qt.Key_K)                                            # play again
        self.assertEqual(g.state, "playing")

    # ------------------------------------------------------------ browsing
    def test_browse_left_right_and_bumpers(self):
        bar = self.bar()
        g = self.open(bar)
        self.key(Qt.Key_Right)                                        # older: shot0
        self.assertEqual(g.current().path, self.shot0)
        self.assertTrue(g.panel.w["counter"].hasFocus())
        self.settle_items(g)
        self.assertEqual(g.state, "shown")
        self.assertIsNotNone(g.frame)
        self.assertEqual(g.panel.w["counter"].text(), "2 / 5")
        self.assertTrue(g.panel.clipbox.isHidden() and not g.panel.shotbox.isHidden())
        self.assertIn("1920×1080", g.panel.w["dims"].text())
        self.assertIn("PNG", g.panel.w["dims"].text())
        self.assertEqual(g.footer.hint, self.gallery_mod.SHOT_HINT)
        bar.on_pad_action("next_section")                             # RB: clip1
        bar.on_pad_action("next_section")                             # RB: shot1
        bar.on_pad_action("prev_section")                             # LB: clip1 (fast: one load)
        self.assertEqual(g.current().path, self.clip1)
        n = len(self.player.sources())
        pump(self.app, 0.05)
        self.assertEqual(len(self.player.sources()), n)               # debounced...
        self.settle_items(g)
        self.assertEqual(self.player.sources()[n:], [str(self.clip1)])  # ...then the last one only
        self.assertEqual(g.state, "playing")
        bar.on_pad_action("left")                                     # D-pad too
        self.assertEqual(g.current().path, self.shot0)
        self.key(Qt.Key_End)
        self.assertEqual(g.current().path, self.clip2)
        self.key(Qt.Key_Right)                                        # no wrap
        self.assertEqual(g.current().path, self.clip2)
        self.key(Qt.Key_Home)
        self.assertEqual(g.current().path, self.clip0)

    def test_filters_keep_the_place(self):
        bar = self.bar()
        g = self.open(bar)
        g.step(2)                                                     # clip1 (60 s old)
        self.settle_items(g)
        before = len(self.player.sources())
        self.key(Qt.Key_Down)                                         # Clips: the same clip
        self.assertEqual(g.filter, "clip")
        self.assertEqual(self.names(g), [self.clip0.name, self.clip1.name, self.clip2.name])
        self.assertEqual(g.current().path, self.clip1)
        self.assertEqual(len(self.player.sources()), before)          # it keeps playing
        self.assertEqual(g.panel.w["counter"].text(), "2 / 3")
        self.assertTrue(g.tabs["clip"].hasFocus())
        g.panel.w["counter"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "05-clips-filter-browsing")
        bar.on_pad_action("down")                                     # Screenshots: nearest in time
        self.assertEqual(g.filter, "shot")
        self.assertEqual(g.current().path, self.shot0)
        self.settle_items(g)
        g.panel.w["full"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "06-screenshot")
        self.key(Qt.Key_Down)                                         # the last filter: stays
        self.assertEqual(g.filter, "shot")
        bar.on_pad_action("up")                                       # Clips: a tie -> the newer
        self.assertEqual(g.current().path, self.clip0)
        self.key(Qt.Key_Up)
        self.assertEqual(g.filter, "all")
        self.assertEqual(g.current().path, self.clip0)

    def test_click_filter_and_counter(self):
        bar = self.bar()
        g = self.open(bar)
        QTest.mouseClick(g.tabs["shot"], Qt.LeftButton)
        self.assertEqual(g.filter, "shot")
        self.assertEqual(g.current().path, self.shot0)
        c = g.panel.w["counter"]
        QTest.mouseClick(c, Qt.LeftButton, pos=QPoint(c.width() - 8, c.height() // 2))   # ›
        self.assertEqual(g.current().path, self.shot1)
        QTest.mouseClick(c, Qt.LeftButton, pos=QPoint(8, c.height() // 2))               # ‹
        self.assertEqual(g.current().path, self.shot0)
        self.assertFalse(bar.focus_visible)                           # the mouse hides the ring

    # ------------------------------------------------------------ playback
    def test_seek_clamped(self):
        bar = self.bar()
        g = self.open(bar)
        bar.on_pad_action("left_trigger")                             # at 0: stays 0
        self.assertEqual(self.player.calls[-1], ("setPosition", 0))
        self.assertTrue(g.panel.w["back10"].hasFocus())
        for _ in range(7):
            bar.on_pad_action("right_trigger")
        seeks = [c[1] for c in self.player.calls if isinstance(c, tuple) and c[0] == "setPosition"]
        self.assertEqual(seeks[-7:], [10_000, 20_000, 30_000, 40_000, 50_000, 60_000, 60_000])
        self.assertTrue(g.panel.w["fwd10"].hasFocus())
        self.key(Qt.Key_J)
        self.assertEqual(self.player.calls[-1], ("setPosition", 50_000))
        self.key(Qt.Key_L)
        self.assertEqual(self.player.calls[-1], ("setPosition", 60_000))
        s = g.panel.w["scrub"]                                        # click on the scrubber
        QTest.mouseClick(s, Qt.LeftButton, pos=QPoint(6 + (s.width() - 12) // 4, s.height() // 2))
        self.assertAlmostEqual(self.player.calls[-1][1] / 1000, 15.0, delta=0.2)

    def test_triggers_from_a_controller(self):
        """LT / RT on a (fake) pad reach the gallery as -10 / +10 s."""
        made = []

        def factory(**kw):
            dev = gamepad.FakeDevice(name="Test pad", path="/fake/gallery-pad",
                                     keys=gamepad.FakeDevice.XBOX_KEYS + (gamepad.BTN_TL2, gamepad.BTN_TR2),
                                     axes={})
            made.append(dev)
            return gamepad.Gamepads(lister=lambda: [dev.path], opener=lambda _p: dev, hotplug="off",
                                    watchdog_thread=False, **kw)
        self.addCleanup(setattr, overlay, "PAD_FACTORY", overlay.PAD_FACTORY)
        overlay.PAD_FACTORY = factory
        cfg = config.default_path()
        self.assertTrue(str(cfg).startswith(os.environ["MOMENTO_TEST_SANDBOX"]))
        bar = self.bar()
        self.addCleanup(lambda: [d.close() for d in made])
        self.wait_for(lambda: bar.pads is not None)
        g = self.open(bar)

        def press(code):
            made[-1].push(gamepad.EV_KEY, code, 1)
            pump(self.app, 0.03)
            made[-1].push(gamepad.EV_KEY, code, 0)
            pump(self.app, 0.03)
        press(gamepad.BTN_TR2)
        self.assertEqual(self.player.calls[-1], ("setPosition", 10_000))
        press(gamepad.BTN_TL2)
        self.assertEqual(self.player.calls[-1], ("setPosition", 0))
        press(gamepad.BTN_X)                                          # X: sound on
        self.assertFalse(g.muted)
        press(gamepad.BTN_Y)                                          # Y: full screen
        self.assertIsNotNone(g.full)
        press(gamepad.BTN_EAST)                                       # B: back to the panel
        self.assertIsNone(g.full)
        press(gamepad.BTN_EAST)                                       # B: back to the clip view
        self.assertEqual(bar.mode, "clip")

    def test_mute_attaches_audio_only_when_on(self):
        bar = self.bar()
        g = self.open(bar)
        bar.on_pad_action("pause")                                    # X: sound on
        self.assertFalse(g.muted)
        self.assertEqual(len(FakeAudio.made), 1)
        self.assertEqual(self.player.calls[-1], ("audio", FakeAudio.made[0]))
        self.assertEqual(g.panel.w["mute"].kind, "sound")
        self.assertTrue(g.panel.w["mute"].hasFocus())
        g.step(2)                                                     # another clip: still on
        self.settle_items(g)
        self.assertFalse(g.muted)
        self.assertIs(self.player.audio, FakeAudio.made[0])
        self.key(Qt.Key_M)                                            # off: the output goes
        self.assertTrue(g.muted)
        self.assertEqual(self.player.calls[-1], ("audio", None))
        self.assertIsNone(g.audio)
        self.key(Qt.Key_M)
        self.key(Qt.Key_Escape)                                       # close, open again: muted
        self.assertEqual(bar.mode, "clip")
        g = self.open(bar)
        self.assertTrue(g.muted)
        self.assertEqual(len(FakePlayer.made), 2)                     # a new player per open
        self.assertIsNone(self.player.audio)

    def test_end_stays_on_last_frame_and_replays(self):
        bar = self.bar()
        g = self.open(bar)
        self.player.end()
        self.assertEqual(g.state, "ended")
        self.assertIsNotNone(g.frame)                                 # the last frame stays
        self.assertEqual(g.panel.w["play"].kind, "replay")
        self.assertEqual(g.current().path, self.clip0)                # no auto-advance
        self.assertTrue(bar.idle.isActive())
        self.shot(bar, "10-clip-ended")
        self.key(Qt.Key_Return)                                       # again from the start
        self.assertIn(("setPosition", 0), self.player.calls[-3:])
        self.assertEqual(self.player.calls[-1], "play")
        self.assertEqual(g.state, "playing")

    def test_clip_that_cannot_play(self):
        FakePlayer.fail = True
        bar = self.bar()
        g = self.open(bar)
        self.assertEqual(g.state, "error")
        self.assertEqual(g.message, "Can't play this clip here")
        self.assertFalse(g.panel.w["play"].isEnabled())
        self.key(Qt.Key_Space)                                        # nothing happens, no crash
        self.key(Qt.Key_L)
        self.assertTrue(bar.idle.isActive())
        self.shot(bar, "11-clip-error")
        self.key(Qt.Key_Right)                                        # a screenshot still shows
        self.settle_items(g)
        self.assertEqual(g.state, "shown")

    def test_idle_off_while_playing(self):
        bar = self.bar()
        g = self.open(bar)
        self.assertEqual(g.state, "playing")
        self.assertFalse(bar.idle.isActive())                         # never hides mid-clip
        self.key(Qt.Key_Right)                                        # input doesn't restart it
        self.key(Qt.Key_Left)
        self.settle_items(g)
        self.assertEqual(g.state, "playing")
        self.assertFalse(bar.idle.isActive())
        self.key(Qt.Key_Space)                                        # paused: 60 s
        self.assertTrue(bar.idle.isActive())
        self.assertEqual(bar.idle.interval(), overlay.GALLERY_IDLE_MS)
        self.key(Qt.Key_Escape)
        self.assertEqual(bar.idle.interval(), overlay.IDLE_CLOSE_MS)  # the clip view's own
        self.assertTrue(bar.idle.isActive())

    def test_pads_renewed_while_open(self):
        bar = self.bar()
        hub = FakeHub()
        bar.pads = hub
        self.addCleanup(setattr, bar, "pads", None)
        self.assertFalse(bar.pad_renew.isActive())
        self.open(bar)
        self.assertTrue(bar.pad_renew.isActive())
        self.assertEqual(bar.pad_renew.interval(), 15_000)
        bar.pad_renew.timeout.emit()
        self.assertEqual(hub.renewed, 1)
        self.key(Qt.Key_Escape)
        self.assertFalse(bar.pad_renew.isActive())
        bar.renew_pads()                                              # not in the gallery: no renew
        self.assertEqual(hub.renewed, 1)

    # ------------------------------------------------------------ full screen
    def test_full_screen_and_back_step_by_step(self):
        bar = self.bar()
        g = self.open(bar)
        self.player.advance(12_000)
        bar.on_pad_action("settings")                                 # Y
        self.assertIsNotNone(g.full)
        view = g.full
        self.assertTrue(view.isVisible())
        self.assertTrue(view.isFullScreen())                          # the window fallback offscreen
        self.assertTrue(view.strips["clip"].isVisible())
        self.assertFalse(view.strips["shot"].isVisible())
        self.assertTrue(g.fullc.w["play"].hasFocus())
        self.assertEqual(g.fullc.w["full"].kind, "unfull")
        strip = view.strips["clip"]
        self.assertEqual(strip.width(), view.width() - 64)
        self.assertEqual(view.height() - strip.geometry().bottom() - 1, 32)
        self.full_shot(view, "07-fullscreen-clip")
        self.key(Qt.Key_Right)                                        # browsing works full screen
        self.settle_items(g)
        self.assertTrue(g.is_shot())
        self.assertTrue(view.strips["shot"].isVisible())
        self.assertTrue(g.fullc.w["counter"].hasFocus())             # browsing: on the counter
        g.fullc.w["shotfull"].setFocus()
        pump(self.app, 0.05)
        g._load_image(g.current(), QSize(1920, 1080))               # as sharp as on a real screen
        self.settle_items(g)
        self.full_shot(view, "08-fullscreen-screenshot")
        self.key(Qt.Key_Left)
        g.chrome_timer.timeout.emit()                                 # 2.5 s without input
        self.assertFalse(g.chrome)
        self.assertFalse(view.strips["clip"].isVisible())
        self.key(Qt.Key_J)                                            # any input brings it back
        self.assertTrue(g.chrome)
        self.key(Qt.Key_Escape)                                       # B: full screen -> panel
        pump(self.app, 0.05)
        self.assertIsNone(g.full)
        self.assertEqual(bar.mode, "gallery")
        self.assertTrue(g.panel.w["full"].hasFocus())
        self.key(Qt.Key_F)                                            # F: full screen again
        self.assertIsNotNone(g.full)
        self.key(Qt.Key_F)
        self.assertIsNone(g.full)
        self.key(Qt.Key_Backspace)                                    # panel -> clip view
        self.assertEqual(bar.mode, "clip")
        self.assertTrue(bar.gallery_btn.hasFocus())
        self.assertIsNone(g.player)
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)

    def test_screenshot_a_opens_full_screen(self):
        bar = self.bar()
        g = self.open(bar)
        g.set_filter("shot")
        self.settle_items(g)
        self.assertTrue(g.tabs["shot"].hasFocus())
        bar.on_pad_action("accept")                                   # A on a screenshot
        self.assertIsNotNone(g.full)
        self.settle_items(g)
        self.assertEqual(g.frame.width(), min(1920, g.full.width()))  # read again at the screen's size
        bar.on_pad_action("accept")
        self.assertIsNone(g.full)

    # ------------------------------------------------------------ empty, close, lazy
    def test_empty_folder_hint(self):
        bar = self.bar(self.empty)
        h0 = bar.height()
        self.key(Qt.Key_G)
        self.wait_for(lambda: not bar.hintbar.isHidden())
        self.assertEqual(bar.mode, "clip")
        self.assertIsNone(bar.gallery.player)
        self.assertIn("No clips or screenshots yet. Saved ones show up here.", bar.hintbar.text())
        self.assertEqual(bar.height(), h0 + overlay.HINT_H + 1)
        self.assertTrue(bar.gallery_btn.hasFocus())
        self.shot(bar, "09-empty")

    def test_dismiss_tears_down_and_next_show_is_clip_view(self):
        bar = self.bar()
        bar.resident = True
        g = self.open(bar)
        self.key(Qt.Key_M)
        player = self.player
        bar.on_pad_action("settings")
        self.assertIsNotNone(g.full)
        view = g.full
        bar.dismiss()                                                 # the hotkey / idle / B at the top
        self.assertFalse(bar.isVisible())
        self.assertIsNone(g.player)
        self.assertIsNone(g.audio)
        self.assertIsNone(g.full)
        self.assertFalse(view.isVisible())
        self.assertIn("stop", player.calls)
        self.assertIn(("audio", None), player.calls)
        self.assertFalse(bar.pad_renew.isActive())
        self.assertFalse(g.active)
        bar.present()
        pump(self.app, 0.05)
        self.assertEqual((bar.mode, bar.stack.currentIndex()), ("clip", 0))
        self.assertTrue(bar.gallery_host.isHidden())
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)
        self.assertIsNone(g.player)

    def test_nothing_survives_close(self):
        """Player, sink, audio output, full screen view, pictures, listing and caches all go."""
        import gc
        import weakref

        import shiboken6
        from PySide6.QtCore import QEvent

        bar = self.bar()
        g = self.open(bar)
        self.key(Qt.Key_M)                                            # an audio output exists
        self.key(Qt.Key_F)                                            # and a full screen view
        objs = {"player": g.player, "sink": g.sink, "audio": g.audio, "full": g.full}
        self.key(Qt.Key_Right)                                        # a screenshot, read in a worker
        self.settle_items(g)
        self.assertEqual(g.state, "shown")
        FakePlayer.made.clear()                                       # the test's own strong references
        FakeAudio.made.clear()
        self.key(Qt.Key_Escape)
        self.key(Qt.Key_Escape)                                       # full screen -> panel -> clip view
        self.assertEqual(bar.mode, "clip")
        pump(self.app, 0.1)
        # What app.exec() does with their deleteLater(); only for these objects, because
        # flushing every pending deletion would also hit what earlier tests left behind.
        for o in objs.values():
            self.app.sendPostedEvents(o, QEvent.DeferredDelete)
        self.assertEqual({k: shiboken6.isValid(o) for k, o in objs.items()}, dict.fromkeys(objs, False))
        refs = {k: weakref.ref(o) for k, o in objs.items()}
        del objs, o
        gc.collect()
        self.assertEqual({k: r() is None for k, r in refs.items()}, dict.fromkeys(refs, True))
        self.assertIsNone(g.frame)
        self.assertEqual((g.items, g.view, g.durations, g.dims), ([], [], {}, {}))
        self.assertEqual(g.window_pills(), [])
        self.assertEqual([w for w in self.app.topLevelWidgets() if isinstance(w, g.W.FullView)], [])

    def test_caches_are_bounded(self):
        g = self.open(self.bar())
        for i in range(self.gallery_mod.CACHE_SIZE + 20):
            g._remember(g.durations, (f"/c/{i}.mp4", 0.0), float(i))
        self.assertEqual(len(g.durations), self.gallery_mod.CACHE_SIZE)
        self.assertNotIn(("/c/0.mp4", 0.0), g.durations)              # the oldest went first

    def test_hidden_bar_never_plays(self):
        bar = self.bar()
        g = self.open(bar)
        bar.hide()                                                    # a one-shot bar hiding
        self.assertIsNone(g.player)
        self.assertFalse(g.active)

    def test_bar_does_not_load_multimedia(self):
        """The resident bar's idle memory is unchanged: QtMultimedia loads with the gallery only."""
        code = textwrap.dedent("""
            import sys
            from tests import _sandbox
            from PySide6.QtWidgets import QApplication
            app = QApplication(["t"])
            from momento import ipc, overlay
            ipc.request = lambda *a, **k: {"ok": True, "state": "recording", "recording": True,
                                           "buffered": 60.0, "max_seconds": 3600}
            Bar, fetch_status = overlay._build([])
            bar = Bar()
            bar.apply_status(fetch_status(timeout=1.0))
            bar.show()
            app.processEvents()
            bar.grab()
            print("mm" if "PySide6.QtMultimedia" in sys.modules else "-",
                  "gal" if "momento.gallery" in sys.modules else "-")
        """)
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        r = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parent.parent),
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[-2:], ["-", "-"])


@unittest.skipIf(QMediaPlayer is None or not shutil.which("ffmpeg"), "needs ffmpeg and QtMultimedia")
class GalleryLive(unittest.TestCase):
    """The real QMediaPlayer on a 2 s clip made with ffmpeg (muted: no audio output)."""

    make = base.OverlayOffscreen.make
    wait_for = base.OverlayOffscreen.wait_for
    key = base.OverlayOffscreen.key

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        cls._orig = ipc.request
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ["MOMENTO_TEST_SANDBOX"])
        cls.out = Path(cls._tmp.name)
        cls.clip = cls.out / "Replay_live_2s.mp4"
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                            "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-y", str(cls.clip)],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                                "-t", "2", "-pix_fmt", "yuv420p", "-y", str(cls.clip)], capture_output=True, timeout=60)
        cls.ok = r.returncode == 0 and cls.clip.exists()

    @classmethod
    def tearDownClass(cls):
        ipc.request = cls._orig
        cls._tmp.cleanup()

    def test_first_frame_and_clamped_seek(self):
        if not self.ok:
            self.skipTest("ffmpeg could not make a test clip")
        from momento import gallery

        self.assertIsNone(gallery.PLAYER_FACTORY)                     # the real player
        bar = self.make(FakeDaemon(True, extra={"output_dir": str(self.out)}))
        self.addCleanup(bar.close_gallery)
        t0 = time.monotonic()
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        try:
            self.wait_for(lambda: g.frame is not None or g.state == "error", timeout=10)
        except AssertionError:
            self.skipTest("no video frame from QtMultimedia here")
        if g.state == "error":
            self.skipTest("QtMultimedia cannot decode the test clip here")
        sys.stderr.write(f"\n  gallery: G -> first frame {(time.monotonic() - t0) * 1000:.0f} ms\n")
        self.assertIsNone(g.audio)                                    # muted: no audio output
        self.wait_for(lambda: g.duration > 0, timeout=5)
        self.assertAlmostEqual(g.duration, 2.0, delta=0.2)
        g.seek(10)                                                    # +10 s on a 2 s clip
        self.assertLessEqual(g.position, g.duration)
        self.wait_for(lambda: g.state in ("ended", "paused", "playing"), timeout=5)
        self.key(Qt.Key_Escape)
        self.assertIsNone(g.player)


if __name__ == "__main__":
    unittest.main()
