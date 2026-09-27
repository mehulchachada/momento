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
from unittest import mock
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_WAYLAND_SHELL_INTEGRATION", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QRectF, QSize, Qt, Signal  # noqa: E402
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
    no_picture = False   # plays, but no frame ever comes (no decoder for the clip's format)

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
        if self.sink is not None and not FakePlayer.no_picture:
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
        self.addCleanup(setattr, gallery, "ANIMATE", gallery.ANIMATE)
        gallery.ANIMATE = False       # end states at once; the motion tests turn it on
        # clip lengths without ffprobe: the fake files are not real MP4s
        self.addCleanup(setattr, gallery.media, "clip_duration", gallery.media.clip_duration)
        gallery.media.clip_duration = lambda path, timeout=3.0: {"Replay_a_1m.mp4": 60.0,
                                                                  "Replay_c_30s.mp4": 30.0}.get(Path(path).name)

    # ------------------------------------------------------------ helpers
    def daemon(self, folder=None, **kw):
        return FakeDaemon(True, extra={"output_dir": str(folder or self.out)}, **kw)

    def bar(self, folder=None, daemon=None):
        bar = self.make(daemon or self.daemon(folder))
        bar.focus_visible = True
        self.addCleanup(self.drain, bar)          # after close_gallery (cleanups run last first)
        self.addCleanup(bar.close_gallery)
        return bar

    @staticmethod
    def drain(bar):
        """Let the bar's gallery pause / resume requests reach this test's fake daemon, not
        the next test's (ipc.request is swapped per test)."""
        jobs, bar._gallery_jobs = bar._gallery_jobs, None
        if jobs is not None:
            jobs.shutdown(wait=True)

    def open(self, bar):
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        pump(self.app, 0.05)
        return bar.gallery

    def screen_size(self, w=1920, h=1080):
        """The screen's available size as the gallery sees it (the offscreen one is short)."""
        from PySide6.QtCore import QRect

        cls = type(QApplication.primaryScreen())
        self.addCleanup(setattr, cls, "availableGeometry", cls.availableGeometry)
        cls.availableGeometry = lambda _s: QRect(0, 0, w, h)

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
        self.screen_size()                                            # 1920x1080
        bar = self.bar()
        h0 = bar.height()
        bar.move(100, 900)
        bottom = bar.y() + bar.height()
        QTest.mouseClick(bar.gallery_btn, Qt.LeftButton)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        self.assertEqual(bar.height(), h0 + g.panel_height() + overlay.GALLERY_JOIN)
        self.assertEqual(bar.height(), 774)                           # panel 719, a hairline, the bar 54
        self.assertEqual((g.stage.width(), g.stage.height()), (1006, 566))
        self.assertEqual(bar.y() + bar.height(), bottom)              # grew upward
        self.assertEqual(bar.width(), 1040)                           # the panel is as wide as the bar
        self.assertFalse(bar.gallery_host.isHidden())
        self.assertTrue(bar.panel.isHidden())

    def test_bar_stays_under_the_gallery(self):
        """The panel opens above the bar; the bar row keeps its layout, buttons and live time
        (window mode: in Full screen the gallery pauses, see test_full_screen_pauses_while_open)."""
        bar = self.resident(daemon=FakeDaemon(True, extra={"output_dir": str(self.out), "target": "window"}))
        opts = [(o.geometry(), o.isEnabled()) for o in bar.options]
        g = self.open(bar)
        self.assertEqual(bar.stack.currentIndex(), 0)                 # the clip lengths, not a footer
        self.assertEqual([(o.geometry(), o.isEnabled()) for o in bar.options], opts)
        self.assertTrue(all(o.isVisible() for o in bar.options))
        self.assertTrue(bar.controls[0]["pause"].isVisible() and bar.controls[0]["gear"].isEnabled())
        self.assertTrue(bar.gallery_btn.selected())                   # the button shows it is open
        host, stack = bar.gallery_host, bar.stack
        self.assertEqual(host.geometry().top(), 1)                    # the panel on top...
        self.assertEqual(stack.geometry().top() - host.geometry().bottom() - 1,
                         overlay.GALLERY_JOIN)                         # ...a hairline, then the bar
        join = bar.gallery_join
        self.assertTrue(join.isVisible())
        self.assertEqual((join.geometry().top(), join.height()), (host.geometry().bottom() + 1, 1))
        self.assertEqual(bar.height() - stack.geometry().bottom() - 1, 1)
        self.assertEqual(g.footer.window(), bar)                      # the footer is in the panel
        self.assertTrue(host.isAncestorOf(g.footer))
        self.assertTrue(g.stage.hasFocus())                           # the stage row first
        st = dict(bar.last_status, buffered_live=754.0)               # the bar row stays live
        bar.apply_status(st)
        self.assertEqual(bar.time.text(), "12:34")
        self.assertTrue(g.stage.hasFocus())                           # ...without taking the keyboard
        self.assertEqual(bar.mode, "gallery")
        from PySide6.QtCore import QEvent
        self.app.sendEvent(g.stage, QEvent(QEvent.Enter))             # the pointer on the panel is on the bar
        self.assertFalse(bar.leave.isActive())
        QTest.mouseClick(bar.gallery_btn, Qt.LeftButton)              # the button toggles it closed
        self.assertEqual(bar.mode, "clip")
        self.assertFalse(bar.gallery_btn.selected())
        self.assertIsNone(g.player)
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)
        self.open(bar)
        self.key(Qt.Key_Escape)                                       # Esc: the gallery first...
        self.assertEqual(bar.mode, "clip")
        self.assertTrue(bar.isVisible())
        self.key(Qt.Key_Escape)                                       # ...then the bar
        self.assertFalse(bar.isVisible())
        pump(self.app, 0.05)
        self.assertEqual(self.exits, [config.BAR_RECYCLE_EXIT])      # and the recycle after a gallery

    def test_bar_buttons_under_the_gallery(self):
        """Pause works with the gallery open; settings / a save fold the gallery away first.
        (Window mode: in Full screen the gallery holds a pause of its own, tested below.)"""
        d = FakeDaemon(True, extra={"output_dir": str(self.out), "target": "window"})
        bar = self.bar(daemon=d)
        g = self.open(bar)
        QTest.mouseClick(bar.controls[0]["pause"], Qt.LeftButton)
        self.wait_for(lambda: d.controls == ["pause"] and not bar.control_busy)
        pump(self.app, 0.05)
        self.assertEqual(bar.mode, "gallery")                         # still open, still playing
        self.assertEqual(g.state, "playing")
        self.assertEqual(bar.controls[0]["pause"].kind, "play")
        QTest.mouseClick(bar.gear, Qt.LeftButton)                     # settings take the bar
        self.assertIsNone(g.player)
        self.wait_for(lambda: bar.mode == "settings")
        self.assertTrue(bar.gallery_host.isHidden())
        bar.close_settings()
        pump(self.app, 0.05)
        g = self.open(bar)
        QTest.mouseClick(bar.options[0], Qt.LeftButton)               # a save: the gallery folds, then saves
        self.assertEqual(bar.mode, "clip")
        self.assertTrue(bar.saving)
        self.assertIsNone(g.player)
        self.wait_for(lambda: d.saves == [bar.options[0].seconds])

    def test_short_screen_shrinks_the_stage(self):
        """1280x720 (the Ally at 150 %): the stage gets smaller so the whole bar fits."""
        bar = self.bar()
        g = self.open(bar)
        self.key(Qt.Key_Escape)
        self.screen_size(1280, 720)
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
        self.assertTrue(g.stage.hasFocus())                           # left / right browse at once
        self.assertEqual(g.panel.w["counter"].text(), "1 / 5")
        self.assertTrue(g.tabs["all"].selected())
        self.assertIn("Clip", g.footer.meta.text())
        self.assertIn("1:00", g.footer.meta.text())
        self.assertIn("Today", g.footer.meta.text())
        self.assertEqual(g.footer.hint, self.gallery_mod.CLIP_KEYS)   # no controller: the keys
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
        self.assertTrue(g.stage.hasFocus())                           # the focus stays on the stage
        self.settle_items(g)
        self.assertEqual(g.state, "shown")
        self.assertIsNotNone(g.frame)
        self.assertEqual(g.panel.w["counter"].text(), "2 / 5")
        self.assertTrue(g.panel.clipbox.isHidden() and not g.panel.shotbox.isHidden())
        self.assertIn("1920×1080", g.panel.w["dims"].text())
        self.assertIn("PNG", g.panel.w["dims"].text())
        self.assertEqual(g.footer.hint, self.gallery_mod.SHOT_KEYS)
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
        self.key(Qt.Key_Up)                                           # the filter row
        self.key(Qt.Key_Right)                                        # Clips: the same clip
        self.assertEqual(g.filter, "clip")
        self.assertEqual(self.names(g), [self.clip0.name, self.clip1.name, self.clip2.name])
        self.assertEqual(g.current().path, self.clip1)
        self.assertEqual(len(self.player.sources()), before)          # it keeps playing
        self.assertEqual(g.panel.w["counter"].text(), "2 / 3")
        self.assertTrue(g.tabs["clip"].hasFocus())
        g.panel.w["counter"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "05-clips-filter-browsing")
        g.set_row("filter")
        bar.on_pad_action("right")                                    # Screenshots: nearest in time
        self.assertEqual(g.filter, "shot")
        self.assertEqual(g.current().path, self.shot0)
        self.settle_items(g)
        g.panel.w["full"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "06-screenshot")
        g.set_row("filter")
        self.key(Qt.Key_Right)                                        # the last filter: stays
        self.assertEqual(g.filter, "shot")
        bar.on_pad_action("left")                                     # Clips: a tie -> the newer
        self.assertEqual(g.current().path, self.clip0)
        self.key(Qt.Key_Left)
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
        self.assertTrue(g.stage.hasFocus())                           # LT / RT keep the focus where it is
        for _ in range(7):
            bar.on_pad_action("right_trigger")
        seeks = [c[1] for c in self.player.calls if isinstance(c, tuple) and c[0] == "setPosition"]
        self.assertEqual(seeks[-7:], [10_000, 20_000, 30_000, 40_000, 50_000, 60_000, 60_000])
        self.assertTrue(g.stage.hasFocus())
        self.key(Qt.Key_J)
        self.assertEqual(self.player.calls[-1], ("setPosition", 50_000))
        self.key(Qt.Key_L)
        self.assertEqual(self.player.calls[-1], ("setPosition", 60_000))
        s = g.panel.w["scrub"]                                        # click on the scrubber
        QTest.mouseClick(s, Qt.LeftButton, pos=QPoint(6 + (s.width() - 12) // 4, s.height() // 2))
        self.assertAlmostEqual(self.player.calls[-1][1] / 1000, 15.0, delta=0.2)

    def pad_bar(self):
        """A bar with one (fake) Xbox-layout pad connected; returns (bar, [devices])."""
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
        return bar, made

    def test_triggers_from_a_controller(self):
        """LT / RT on a (fake) pad reach the gallery as -10 / +10 s."""
        bar, made = self.pad_bar()
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

    def test_hints_follow_the_controller(self):
        """A connected controller: its buttons in the hints; unplugged: the keys."""
        gm = self.gallery_mod
        self.screen_size()
        bar, made = self.pad_bar()
        self.assertTrue(bar.pad_connected())
        g = self.open(bar)
        self.assertEqual(g.footer.hint, gm.CLIP_HINT)
        self.assertIn((["←", "→"], "browse"), gm.CLIP_HINT)          # the D-pad / stick on the stage
        self.key(Qt.Key_M)                                            # sound on: the speaker shows it
        self.player.advance(21_000)
        g.panel.w["play"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "14-above-clip-controller")
        self.key(Qt.Key_F)
        chips = g.full.findChildren(g.W.Chips)
        self.assertEqual([c.tokens for c in chips], [gm.BACK_HINT, gm.BACK_HINT])
        self.key(Qt.Key_F)
        bar.pads.remove_device(made[-1].path)                         # unplugged: the keys, at once
        self.assertFalse(bar.pad_connected())
        self.assertEqual(g.footer.hint, gm.PLAYER_KEYS)               # (the player row has the focus)
        self.key(Qt.Key_PageDown)                                     # a screenshot
        self.settle_items(g)
        self.assertEqual(g.footer.hint, gm.SHOT_KEYS)
        self.key(Qt.Key_F)
        self.assertEqual([c.tokens for c in g.full.findChildren(g.W.Chips)], [gm.BACK_KEYS, gm.BACK_KEYS])

    def two_pad_bar(self, pads=("ps", "xbox")):
        """A bar with fake pads: "ps" (a DualSense on hid-playstation) and/or "xbox"; fresh
        devices on every open, like re-opening /dev/input. Returns (bar, {kind: [devices]})."""
        made = {k: [] for k in pads}

        def make(kind):
            if kind == "ps":
                return gamepad.FakeDevice(name="DualSense Wireless Controller", path="/fake/dualsense",
                                          keys=gamepad.FakeDevice.XBOX_KEYS + (gamepad.BTN_TL2, gamepad.BTN_TR2),
                                          axes={}, vendor=0x054c, driver="playstation")
            return gamepad.FakeDevice(name="Microsoft X-Box One Elite 2 pad", path="/fake/elite",
                                      keys=gamepad.FakeDevice.XBOX_KEYS + (gamepad.BTN_TL2, gamepad.BTN_TR2),
                                      axes={}, vendor=0x045e, driver="xpad")

        def factory(**kw):
            devs = {}
            for kind in pads:
                devs[kind] = make(kind)
                made[kind].append(devs[kind])
            by_path = {d.path: d for d in devs.values()}
            return gamepad.Gamepads(lister=lambda: list(by_path), opener=by_path.get, hotplug="off",
                                    watchdog_thread=False, **kw)
        self.addCleanup(setattr, overlay, "PAD_FACTORY", overlay.PAD_FACTORY)
        overlay.PAD_FACTORY = factory
        bar = self.bar()
        self.addCleanup(lambda: [d.close() for devs in made.values() for d in devs])
        self.wait_for(lambda: bar.pads is not None)
        return bar, made

    def touch(self, dev, code=gamepad.BTN_THUMBL):
        """A press that does nothing in the bar (a stick click) but marks the pad as in use."""
        dev.push(gamepad.EV_KEY, code, 1)
        pump(self.app, 0.03)
        dev.push(gamepad.EV_KEY, code, 0)
        pump(self.app, 0.03)

    def test_hints_with_a_playstation_pad(self):
        """A DualSense: ✕ ○ □ △ by position, L1 / R1, L2 / R2, drawn as chips."""
        gm = self.gallery_mod
        self.screen_size()
        bar, made = self.two_pad_bar(("ps",))
        self.assertEqual(bar.pad_symbols(), "playstation")           # the only pad: in use already
        g = self.open(bar)
        self.assertEqual(g.footer.hint, [(["←", "→"], "browse"), (["✕"], "play"),
                                         (["L2", "R2"], "10 s"), (["□"], "sound"), (["△"], "full screen")])
        self.player.advance(21_000)
        g.panel.w["play"].setFocus()
        pump(self.app, 0.05)
        self.shot(bar, "16-above-clip-playstation")
        self.key(Qt.Key_F)
        self.assertEqual([c.tokens for c in g.full.findChildren(g.W.Chips)], [[(["○"], "Back")]] * 2)
        self.key(Qt.Key_F)
        self.key(Qt.Key_PageDown)                                     # a screenshot
        self.settle_items(g)
        self.assertEqual(g.footer.hint, gm.pad_hint(gm.PAD_SHOT, "playstation"))
        self.assertEqual(g.footer.hint[-1], (["△"], "full screen"))
        # the symbols are drawn: each chip is a round 18 px one with a light glyph in its middle
        self.assertEqual(gm.chip_run(None, 0, 0, [(["✕"], "")]), gm.chip_run(None, 0, 0, [(["A"], "")]))
        for sym in gm.PS_GLYPHS:
            img = QImage(24, 24, QImage.Format_ARGB32)
            img.fill(QColor("#000000"))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            gm.chip_run(p, 3, 12, [([sym], "")])
            p.end()
            lit = [QColor(img.pixel(x, y)).lightness() for x in range(8, 17) for y in range(7, 17)]
            self.assertGreater(max(lit), 150, sym)                   # the glyph (PILL_SEL) shows

    def test_hints_follow_the_pad_in_use(self):
        """PS and Xbox pads: the hints switch to whichever was pressed last, live, and the
        next open starts with it."""
        gm = self.gallery_mod
        bar, made = self.two_pad_bar()
        g = self.open(bar)
        self.assertEqual(g.footer.hint, gm.CLIP_HINT)                  # two pads, none used yet: Xbox
        self.touch(made["ps"][-1])
        self.assertEqual(bar.pad_symbols(), "playstation")
        self.assertEqual(g.footer.hint, gm.pad_hint(gm.PAD_CLIP, "playstation"))
        self.assertEqual(bar.mode, "gallery")                         # the stick click did nothing else
        pump(self.app, 0.3)
        self.touch(made["xbox"][-1])
        self.assertEqual(g.footer.hint, gm.CLIP_HINT)
        self.assertIn((["A"], "play"), g.footer.hint)
        pump(self.app, 0.3)
        made["ps"][-1].push(gamepad.EV_KEY, gamepad.BTN_NORTH, 1)     # △ (0x133 on hid-playstation): full screen
        pump(self.app, 0.03)
        made["ps"][-1].push(gamepad.EV_KEY, gamepad.BTN_NORTH, 0)
        pump(self.app, 0.03)
        self.assertIsNotNone(g.full)
        self.assertEqual([c.tokens for c in g.full.findChildren(g.W.Chips)], [[(["○"], "Back")]] * 2)
        self.key(Qt.Key_F)
        bar.pads.remove_device(made["ps"][-1].path)                    # unplugged: the other one's
        self.assertEqual(g.footer.hint, gm.CLIP_HINT)
        self.touch(made["xbox"][-1])
        pump(self.app, 0.3)
        # hidden and shown again (fresh devices): the pad last used keeps its symbols
        bar.close_gallery()
        bar.hide()
        pump(self.app, 0.05)
        bar.show()
        self.wait_for(lambda: bar.pads is not None and len(bar.pads.pads) == 2)
        self.assertEqual(bar.pad_symbols(), "xbox")
        self.touch(made["ps"][-1])
        self.assertEqual(bar.pad_symbols(), "playstation")
        bar.hide()
        pump(self.app, 0.05)
        bar.show()
        self.wait_for(lambda: bar.pads is not None and len(bar.pads.pads) == 2)
        self.assertEqual(bar.pad_symbols(), "playstation")

    # ------------------------------------------------------------ the pause (Full screen)
    def test_full_screen_pauses_while_open(self):
        """Full screen: the gallery pauses recording (the label says why) and closing resumes."""
        daemon = self.daemon()
        bar = self.bar(daemon=daemon)
        self.assertEqual(bar.view, "rec")
        g = self.open(bar)
        self.wait_for(lambda: daemon.gallery_controls == ["pause"])
        self.assertEqual(daemon.gallery_pid, os.getpid())
        self.wait_for(lambda: bar.view == "paused")
        self.assertEqual((bar.gallery_pause, bar.pause_reason), ("held", "gallery"))
        self.assertEqual(bar.name.accessibleName(), overlay.GALLERY_PAUSED)
        self.assertEqual(bar.name.text(), overlay.GALLERY_PAUSED)      # whole, not elided
        self.assertEqual(bar.time.text(), "")                          # the sentence takes its room
        self.assertFalse(bar.controls[0]["shot"].isEnabled())           # no frames while paused
        self.assertTrue(any(o.isEnabled() for o in bar.options))       # saving still works
        self.assertEqual(daemon.controls, [])                          # not the user's pause
        pump(self.app, 0.05)
        self.shot(bar, "17-gallery-paused")
        self.key(Qt.Key_Escape)                                        # back to the clip view
        self.assertEqual(bar.mode, "clip")
        self.wait_for(lambda: daemon.gallery_controls == ["pause", "resume"])
        self.wait_for(lambda: bar.view == "rec")
        self.assertFalse(daemon.paused)
        self.assertIsNone(bar.gallery_pause)
        self.assertEqual(bar.name.accessibleName(), "Recording Full Screen")
        del g

    def test_window_mode_keeps_recording(self):
        daemon = FakeDaemon(True, extra={"output_dir": str(self.out), "target": "window",
                                         "target_name": "Ember Rift"})
        bar = self.bar(daemon=daemon)
        self.open(bar)
        pump(self.app, 0.2)
        self.key(Qt.Key_Escape)
        pump(self.app, 0.1)
        self.assertEqual((daemon.gallery_controls, daemon.paused, bar.view), ([], False, "rec"))

    def test_a_user_pause_stays(self):
        daemon = self.daemon(paused=True)
        bar = self.bar(daemon=daemon)
        self.assertEqual(bar.view, "paused")
        self.open(bar)
        pump(self.app, 0.2)
        self.assertEqual(bar.name.accessibleName(), "Paused Full Screen")
        self.key(Qt.Key_Escape)
        pump(self.app, 0.2)
        self.assertEqual((daemon.gallery_controls, daemon.paused), ([], True))

    def test_play_during_the_gallery_takes_over(self):
        """The user presses play while the gallery holds the pause: closing changes nothing."""
        daemon = self.daemon()
        bar = self.bar(daemon=daemon)
        self.open(bar)
        self.wait_for(lambda: bar.view == "paused")
        bar.toggle_pause()                                             # play (the bar row stays live)
        self.wait_for(lambda: daemon.controls == ["resume"])
        self.assertFalse(daemon.paused)
        self.wait_for(lambda: not bar.control_busy and bar.view == "rec")
        bar.toggle_pause()                                             # and the user pauses again
        self.wait_for(lambda: daemon.controls[:2] == ["resume", "pause"])
        self.assertEqual(daemon.controls, ["resume", "pause"])
        self.key(Qt.Key_Escape)
        self.wait_for(lambda: daemon.gallery_controls == ["pause", "resume"])
        pump(self.app, 0.1)
        self.assertTrue(daemon.paused)                                  # the user's pause stays
        self.assertIsNone(daemon.pause_reason)

    def test_hiding_the_bar_resumes(self):
        """The bar hides (idle, the hotkey, before a recycle) with the gallery open."""
        daemon = self.daemon()
        bar = self.bar(daemon=daemon)
        bar.resident = True
        exits = []
        bar.request_exit = exits.append                                # the recycle, not the loop's end
        self.open(bar)
        self.wait_for(lambda: daemon.gallery_controls == ["pause"])
        bar.dismiss()
        self.wait_for(lambda: daemon.gallery_controls[:2] == ["pause", "resume"])
        self.assertEqual(daemon.gallery_controls, ["pause", "resume"], daemon.gallery_controls)
        self.assertFalse(daemon.paused)
        pump(self.app, 0.05)
        self.assertEqual(len(exits), 1)                                # recycled after resuming

    # ------------------------------------------------------------ deleting
    def scratch(self, clips=3, shots=1):
        """A clips folder of our own (these tests delete from it): newest first it reads
        clip0 .. clipN, then the screenshots."""
        d = Path(tempfile.mkdtemp(dir=self._tmp.name))
        (d / "Images").mkdir()
        now = time.time()
        for i in range(clips):
            f = d / f"Replay_{i}.mp4"
            f.write_bytes(b"\0" * 1024)
            os.utime(f, (now - 10 * i, now - 10 * i))
        for i in range(shots):
            f = d / "Images" / f"Momento_{i}.png"
            scene("shot", 320, 180).save(str(f))
            os.utime(f, (now - 1000 - i, now - 1000 - i))
        return d

    def trash_mock(self, can=True):
        """media.can_trash / media.trash stand-ins: the trash call is recorded (with whether
        a player was still holding the file) and removes the file like the real Trash would."""
        gm = self.gallery_mod
        calls = []

        def trash(path):
            calls.append((Path(path).name, self.bar_under_test.gallery.player is None))
            os.remove(path)
        for name, fake in (("can_trash", lambda _p: can), ("trash", trash)):
            patcher = mock.patch.object(gm.media, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def test_delete_asks_first_cancel_is_the_default(self):
        folder = self.scratch()
        bar = self.bar_under_test = self.bar(folder)
        calls = self.trash_mock()
        g = self.open(bar)
        self.key(Qt.Key_Delete)
        f = g.footer
        self.assertTrue(g.asking())
        self.assertEqual(f.question.text(), "Delete this clip?")
        self.assertTrue(f.no.hasFocus())                              # Cancel first
        self.assertFalse(f.trash.isVisible() or f.back.isVisible())
        self.assertEqual(self.player.state, QMediaPlayer.PlaybackState.PausedState)   # paused under it
        pump(self.app, 0.05)
        self.shot(bar, "19-delete-question")
        self.key(Qt.Key_Escape)                                       # Esc: Cancel
        self.assertFalse(g.asking())
        self.assertEqual(bar.mode, "gallery")                         # ...not the gallery closing
        self.assertTrue(f.trash.hasFocus())
        self.key(Qt.Key_Delete)
        bar.on_pad_action("back")                                     # B: Cancel
        self.assertFalse(g.asking())
        self.key(Qt.Key_Delete)
        self.key(Qt.Key_Return)                                       # Enter on Cancel
        self.assertFalse(g.asking())
        self.key(Qt.Key_Delete)
        bar.on_idle()                                                 # idle: Cancel, the bar stays
        self.assertFalse(g.asking())
        self.assertTrue(bar.isVisible())
        self.assertEqual(calls, [])
        self.assertEqual(len(list(folder.glob("*.mp4"))), 3)

    def test_delete_moves_to_the_trash_and_shows_the_next(self):
        folder = self.scratch()
        bar = self.bar_under_test = self.bar(folder)
        calls = self.trash_mock()
        g = self.open(bar)
        self.settle_items(g)
        self.assertEqual(g.current().path.name, "Replay_0.mp4")
        self.assertIsNotNone(g.player)
        g.durations[(str(g.current().path), g.current().mtime)] = 12.0   # something cached for it
        self.key(Qt.Key_Delete)
        self.key(Qt.Key_Left)                                         # Delete
        self.assertTrue(g.footer.yes.hasFocus())
        self.key(Qt.Key_Return)
        self.assertEqual(calls, [("Replay_0.mp4", True)])             # to the Trash, the player let go first
        self.assertFalse(g.asking())
        self.assertEqual(g.current().path.name, "Replay_1.mp4")       # the next one
        self.assertEqual(len(g.items), 3)
        self.assertEqual(g.panel.w["counter"].text(), "1 / 3")
        self.assertFalse(any(k[0].endswith("Replay_0.mp4") for k in g.durations))
        self.assertTrue(g.stage.hasFocus())
        g.set_filter("clip")
        self.assertEqual([i.path.name for i in g.view], ["Replay_1.mp4", "Replay_2.mp4"])

    def test_delete_the_last_one_shows_the_previous_then_empty(self):
        folder = self.scratch(clips=2, shots=0)
        bar = self.bar_under_test = self.bar(folder)
        calls = self.trash_mock()
        g = self.open(bar)
        self.key(Qt.Key_End)                                          # the oldest
        self.settle_items(g)
        g.ask_delete()
        g.confirm_delete()
        self.assertEqual(g.current().path.name, "Replay_0.mp4")       # the previous one
        g.ask_delete()
        g.confirm_delete()
        self.assertIsNone(g.current())
        self.assertEqual((g.state, g.message), ("empty", "Nothing saved yet"))
        self.assertEqual([c[0] for c in calls], ["Replay_1.mp4", "Replay_0.mp4"])
        self.assertFalse(g.footer.trash.isEnabled())
        g.ask_delete()                                                # nothing left to ask about
        self.assertFalse(g.asking())

    def test_delete_without_a_trash_says_so(self):
        folder = self.scratch(clips=1, shots=1)
        bar = self.bar_under_test = self.bar(folder)
        calls = self.trash_mock(can=False)
        g = self.open(bar)
        self.key(Qt.Key_End)                                          # the screenshot
        self.settle_items(g)
        g.ask_delete()
        self.assertEqual(g.footer.question.text(), "Delete this screenshot? It can't be undone.")
        g.confirm_delete()
        self.assertEqual(calls, [])                                   # deleted for good, not trashed
        self.assertEqual(list((folder / "Images").iterdir()), [])
        self.assertEqual(g.current().path.name, "Replay_0.mp4")

    def test_delete_refuses_outside_the_clips_folder(self):
        folder = self.scratch(clips=1, shots=0)
        bar = self.bar_under_test = self.bar(folder)
        calls = self.trash_mock()
        g = self.open(bar)
        g.folder = str(Path(self._tmp.name) / "Elsewhere")            # the item isn't in it
        g.ask_delete()
        g.confirm_delete()
        self.assertEqual(calls, [])
        self.assertTrue((folder / "Replay_0.mp4").exists())
        self.assertEqual(g.footer.meta.text(), self.gallery_mod.DELETE_FAILED)
        self.assertEqual(g.current().path.name, "Replay_0.mp4")

    def test_delete_with_the_controller(self):
        folder = self.scratch()
        bar = self.bar_under_test = self.bar(folder)
        calls = self.trash_mock()
        g = self.open(bar)
        for _ in range(2):
            bar.on_pad_action("down")                                 # stage -> player -> footer
        self.assertEqual(g.row, "footer")
        self.assertTrue(g.footer.trash.hasFocus())
        bar.on_pad_action("accept")                                   # A on the bin: the question
        self.assertTrue(g.footer.no.hasFocus())
        bar.on_pad_action("up")                                       # the question keeps the focus
        self.assertTrue(g.footer.no.hasFocus())
        bar.on_pad_action("left")
        self.assertTrue(g.footer.yes.hasFocus())
        bar.on_pad_action("accept")
        self.assertEqual([c[0] for c in calls], ["Replay_0.mp4"])

    # ------------------------------------------------------------ sounds
    def listen(self, bar):
        """The bar's sounds, recorded by name (nothing is played)."""
        from momento import sfx

        heard, t = [], [0.0]

        class Rec:
            def play(self, name, pcm):
                heard.append(name)

        def clock():
            t[0] += 1.0                   # never a move too soon
            return t[0]
        bar.sounds = sfx.Sounds(backend=Rec, sync=True, clock=clock)
        return heard

    def test_sounds(self):
        """Open / close, moving, choosing, a delete and a refused one; the idle timeout is silent."""
        folder = self.scratch()
        bar = self.bar_under_test = self.bar(folder)
        self.trash_mock()
        heard = self.listen(bar)
        g = self.open(bar)
        self.assertEqual(heard, ["gallery_open"])                     # once it opens, not at the press
        del heard[:]
        self.key(Qt.Key_Right)                                        # the next item
        self.key(Qt.Key_Down)                                         # stage -> player
        self.key(Qt.Key_Return)                                       # play / pause
        self.key(Qt.Key_Home)                                         # the newest again
        bar.on_pad_action("up")                                       # the controller: the same
        self.assertEqual(heard, ["move", "move", "select", "move", "move"])
        del heard[:]
        self.key(Qt.Key_Delete)                                       # the question
        self.key(Qt.Key_Escape)                                       # Cancel
        self.key(Qt.Key_Delete)
        bar.on_idle()                                                 # timed out: no sound
        self.assertEqual(heard, ["select", "select", "select"])
        del heard[:]
        self.key(Qt.Key_Delete)
        self.key(Qt.Key_Left)                                         # Cancel -> Delete
        self.key(Qt.Key_Return)
        self.assertEqual(heard, ["select", "move", "delete"])
        del heard[:]
        g.folder = str(Path(self._tmp.name) / "Elsewhere")            # a delete that is refused
        g.ask_delete()
        g.confirm_delete()
        self.assertEqual(heard, ["select", "error"])
        del heard[:]
        self.key(Qt.Key_Escape)                                       # back to the bar
        self.assertEqual((bar.mode, heard), ("clip", ["gallery_close"]))

    def test_sounds_nothing_saved_yet(self):
        bar = self.bar(self.empty)
        heard = self.listen(bar)
        self.key(Qt.Key_G)
        self.wait_for(lambda: not bar.hintbar.isHidden())
        self.assertEqual(heard, ["select"])

    # ------------------------------------------------------------ focus rows
    def test_rows_up_and_down(self):
        bar = self.bar()
        g = self.open(bar)
        self.assertEqual(g.row, "stage")                              # browsing at once
        self.assertTrue(g.stage.hasFocus())
        self.key(Qt.Key_Up)
        self.assertEqual(g.row, "filter")
        self.assertTrue(g.tabs["all"].hasFocus())
        self.key(Qt.Key_Up)                                           # nothing above
        self.assertEqual(g.row, "filter")
        for want in ("stage", "player", "footer", "footer"):          # never down into the bar row
            self.key(Qt.Key_Down)
            self.assertEqual(g.row, want)
        self.assertTrue(g.footer.trash.hasFocus())
        self.assertEqual(bar.mode, "gallery")
        self.key(Qt.Key_Up)
        self.assertTrue(g.panel.w["play"].hasFocus())                 # the player row: its play button
        self.key(Qt.Key_PageDown)                                     # a screenshot: no player row
        self.settle_items(g)
        self.assertEqual(g.rows(), ("filter", "stage", "footer"))
        self.assertEqual(g.row, "stage")
        self.assertTrue(g.stage.hasFocus())

    def test_left_right_in_each_row(self):
        bar = self.bar()
        g = self.open(bar)
        self.key(Qt.Key_Right)                                        # stage: the next item
        self.assertEqual(g.index, 1)
        self.key(Qt.Key_Left)
        self.settle_items(g)
        self.assertEqual(g.index, 0)
        self.key(Qt.Key_Down)                                         # player: -10 / +10 s
        self.key(Qt.Key_Right)
        self.assertEqual(self.player.calls[-1], ("setPosition", 10_000))
        self.key(Qt.Key_Left)
        self.assertEqual(self.player.calls[-1], ("setPosition", 0))
        self.assertTrue(g.panel.w["play"].hasFocus())                 # the ring stays on play
        self.assertEqual(g.index, 0)
        self.key(Qt.Key_Return)                                       # A / Enter: play / pause
        self.assertEqual(self.player.calls[-1], "pause")
        self.key(Qt.Key_Up)
        self.key(Qt.Key_Up)                                           # filter: left / right switch it
        self.key(Qt.Key_Right)
        self.assertEqual(g.filter, "clip")
        self.assertTrue(g.tabs["clip"].hasFocus())
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Down)                                         # footer: the bin <-> Back
        self.assertTrue(g.footer.trash.hasFocus())
        self.key(Qt.Key_Right)
        self.assertTrue(g.footer.back.hasFocus())
        self.key(Qt.Key_Right)                                        # the last one: stays
        self.assertTrue(g.footer.back.hasFocus())
        self.key(Qt.Key_Return)                                       # A on Back: the gallery closes
        self.assertEqual(bar.mode, "clip")

    def test_bumpers_and_triggers_from_any_row(self):
        bar = self.bar()
        g = self.open(bar)
        self.settle_items(g)
        for _ in range(2):
            bar.on_pad_action("down")                                 # the footer
        bar.on_pad_action("right_trigger")                            # RT: +10 s from here
        self.assertEqual(self.player.calls[-1], ("setPosition", 10_000))
        bar.on_pad_action("next_section")                             # RB: the next item...
        self.assertEqual(g.index, 1)
        self.assertEqual(g.row, "footer")                             # ...the focus stays in its row
        self.assertTrue(g.footer.trash.hasFocus())
        bar.on_pad_action("up")                                       # a screenshot: stage, no player
        self.assertEqual(g.row, "stage")
        bar.on_pad_action("prev_section")
        self.assertEqual(g.index, 0)

    def test_hints_follow_the_focus(self):
        gm = self.gallery_mod
        bar = self.bar()
        g = self.open(bar)
        self.assertEqual(g.footer.hint, gm.CLIP_KEYS)
        self.assertEqual(g.footer.hint[0], (["←", "→"], "browse"))
        self.key(Qt.Key_Down)
        self.assertEqual(g.footer.hint, gm.PLAYER_KEYS)
        self.assertEqual(g.footer.hint[0], (["←", "→"], "10 s"))
        pump(self.app, 0.25)
        self.shot(bar, "20-focus-player")
        self.key(Qt.Key_Up)
        self.key(Qt.Key_Up)
        self.assertEqual(g.footer.hint, gm.FILTER_KEYS)
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Down)
        self.assertEqual(g.footer.hint, gm.FOOTER_KEYS)

    def test_hints_follow_the_focus_with_a_ps_pad(self):
        gm = self.gallery_mod
        self.screen_size()
        bar, made = self.two_pad_bar(("ps",))
        g = self.open(bar)
        self.assertEqual(g.footer.hint[0], (["←", "→"], "browse"))
        bar.on_pad_action("down")
        self.assertEqual(g.footer.hint, gm.pad_hint(gm.PAD_PLAYER, "playstation"))
        self.assertIn((["L1", "R1"], "browse"), g.footer.hint)
        self.assertIn((["✕"], "play"), g.footer.hint)
        self.player.advance(21_000)
        pump(self.app, 0.25)
        self.shot(bar, "21-focus-player-playstation")
        bar.on_pad_action("up")
        bar.on_pad_action("up")
        self.assertEqual(g.footer.hint, gm.pad_hint(gm.PAD_FILTER, "playstation"))

    def test_ring_and_highlight_only_for_keys_and_controllers(self):
        bar = self.bar()
        g = self.open(bar)
        self.assertTrue(bar.focus_visible)
        self.assertEqual(g.glow["stage"], 1.0)                        # the focused row glows
        self.key(Qt.Key_Down)
        self.assertEqual((g.glow["stage"], g.glow["player"]), (0.0, 1.0))
        QTest.mouseClick(g.panel.w["mute"], Qt.LeftButton)            # the mouse: no ring, no glow
        self.assertFalse(bar.focus_visible)
        self.assertEqual(max(g.glow.values()), 0.0)
        self.key(Qt.Key_Up)                                           # a key: back
        self.assertEqual(g.glow["stage"], 1.0)

    def test_highlight_eases_between_rows(self):
        self.motion()
        bar = self.bar()
        g = self.open(bar)
        self.settle_motion(g)
        self.key(Qt.Key_Down)
        self.wait_for(lambda: 0.3 < g.glow["player"] < 0.8, timeout=1)
        self.assertGreater(g.glow["stage"], 0.0)                      # the row left fades meanwhile
        self.assertLess(g.glow["stage"], 1.0)
        g.glow_tween.anim.pause()                                     # a still of the middle, for review
        self.shot(bar, "22-focus-glow-mid")
        g.glow_tween.anim.resume()
        pump(self.app, 0.25)
        self.assertEqual((g.glow["stage"], g.glow["player"]), (0.0, 1.0))
        self.assertEqual(g.stage.size(), g.stage_size)                # nothing relaid out

    def test_full_screen_rows(self):
        bar = self.bar()
        g = self.open(bar)
        self.key(Qt.Key_F)
        self.assertIsNotNone(g.full)
        self.assertEqual(g.rows(), ("stage", "player"))
        self.assertEqual(g.row, "stage")
        self.key(Qt.Key_Down)                                         # the strip's player
        self.assertEqual(g.row, "player")
        self.assertTrue(g.fullc.w["play"].hasFocus())
        self.key(Qt.Key_Right)
        self.assertEqual(self.player.calls[-1], ("setPosition", 10_000))
        self.key(Qt.Key_Up)
        self.assertEqual(g.row, "stage")
        self.key(Qt.Key_Right)                                        # the stage: browse
        self.assertEqual(g.index, 1)
        self.key(Qt.Key_Escape)
        self.assertIsNone(g.full)

    def test_keyboard_hints_without_a_controller(self):
        """No controller (the sandbox opens none): the keys, for a clip and a screenshot."""
        gm = self.gallery_mod
        self.screen_size()
        bar = self.bar()
        self.assertFalse(bar.pad_connected())
        g = self.open(bar)
        self.assertEqual(g.footer.hint, gm.CLIP_KEYS)
        self.assertIn((["M"], "sound"), gm.CLIP_KEYS)
        self.player.advance(21_000)
        pump(self.app, 0.05)
        self.shot(bar, "15-above-clip-keyboard")
        self.key(Qt.Key_Right)                                        # a screenshot, selected
        self.settle_items(g)
        self.assertEqual(g.footer.hint, gm.SHOT_KEYS)
        pump(self.app, 0.05)
        self.shot(bar, "13-above-screenshot-keyboard")
        self.key(Qt.Key_Up)                                           # ↑: the filter row
        self.assertEqual(g.footer.hint, gm.FILTER_KEYS)
        self.key(Qt.Key_Right)                                        # →: the next filter
        self.assertEqual(g.filter, "clip")
        self.key(Qt.Key_F)
        self.assertEqual([c.tokens for c in g.full.findChildren(g.W.Chips)], [gm.BACK_KEYS, gm.BACK_KEYS])
        widths = [gm.chip_run(None, 0, 0, t) for t in (gm.CLIP_HINT, gm.CLIP_KEYS)]
        self.assertLess(max(widths), g.footer.width() - 2 * 240)       # clear of the meta and Back

    def test_mute_attaches_audio_only_when_on(self):
        bar = self.bar()
        g = self.open(bar)
        bar.on_pad_action("pause")                                    # X: sound on
        self.assertFalse(g.muted)
        self.assertEqual(len(FakeAudio.made), 1)
        self.assertEqual(self.player.calls[-1], ("audio", FakeAudio.made[0]))
        self.assertEqual(g.panel.w["mute"].kind, "sound")
        self.assertTrue(g.stage.hasFocus())                           # X doesn't move the focus
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

    def test_mute_button_by_the_transport_and_in_full_screen(self):
        bar = self.bar()
        g = self.open(bar)
        w = g.panel.w
        order = sorted(("back10", "play", "fwd10", "mute", "now"), key=lambda k: w[k].x())
        self.assertEqual(order, ["back10", "play", "fwd10", "mute", "now"])   # right after +10
        self.assertEqual(w["mute"].x(), w["fwd10"].x() + w["fwd10"].width())
        self.assertEqual(w["mute"].accessibleName(), "Turn sound on (M)")
        QTest.mouseClick(w["mute"], Qt.LeftButton)                    # a click: sound on
        self.assertFalse(g.muted)
        self.assertEqual(w["mute"].accessibleName(), "Mute (M)")
        self.key(Qt.Key_F)                                            # full screen keeps it
        f = g.fullc.w
        self.assertEqual(f["mute"].kind, "sound")
        self.assertEqual(f["mute"].x(), f["fwd10"].x() + f["fwd10"].width())
        self.key(Qt.Key_M)                                            # M in full screen
        self.assertTrue(g.muted)
        self.assertIsNone(g.audio)
        bar.on_pad_action("pause")                                    # X / Square in full screen
        self.assertFalse(g.muted)
        self.assertEqual(f["mute"].kind, "sound")
        self.key(Qt.Key_Escape)
        self.assertEqual(w["mute"].kind, "sound")                     # the panel shows it too
        self.assertIn((["X"], "sound"), self.gallery_mod.CLIP_HINT)

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

    def test_clip_without_a_picture(self):
        """A clip whose video can't be decoded here (an AV1 or H.265 clip without a decoder):
        a clear message after a few seconds, not a black box."""
        self.addCleanup(setattr, FakePlayer, "no_picture", False)
        FakePlayer.no_picture = True
        bar = self.bar()
        g = self.open(bar)
        self.assertEqual(g.state, "playing")
        self.player.advance(1000)
        self.assertEqual(g.state, "playing")                          # the first frame may take a moment
        with self.assertLogs("momento.gallery", "WARNING"):
            self.player.advance(2500)
        self.assertEqual(g.state, "error")
        self.assertEqual(g.message, self.gallery_mod.NO_PICTURE)
        self.assertTrue(bar.idle.isActive())

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
        self.assertEqual(bar.idle.interval(), overlay.IDLE_HIDE_MS)  # the clip view's own
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

    # ------------------------------------------------------------ motion
    def motion(self):
        self.gallery_mod.ANIMATE = True
        return self.gallery_mod

    def settle_motion(self, g, seconds=0.45):
        pump(self.app, seconds)

    def test_motion_panel_grows_and_folds(self):
        gm = self.motion()
        bar = self.bar()
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        full_h = overlay.BAR_HEIGHT + 2 + g.panel_height() + overlay.GALLERY_JOIN
        self.assertLess(bar.height(), full_h)                         # growing, not a jump
        self.assertIsNotNone(g.panel_w.graphicsEffect())              # the content fades in
        self.wait_for(lambda: bar.height() == full_h, timeout=2)
        self.settle_motion(g, 0.05)
        self.assertIsNone(g.panel_w.graphicsEffect())                 # the effect goes with the motion
        self.assertEqual(g.panel_w.geometry().bottom(), bar.gallery_host.height() - 1)
        self.assertTrue(g.stage.hasFocus())
        self.key(Qt.Key_Escape)                                       # back: the clip view is live at once
        self.assertEqual(bar.mode, "clip")
        self.assertTrue(g.closing)
        self.assertIsNotNone(g.out_img)                               # a still of the stage while it folds
        self.assertIsNone(g.player)                                   # the player is already gone
        self.wait_for(lambda: bar.gallery_host.isHidden(), timeout=2)   # folded (the end of an
        self.assertEqual(bar.height(), overlay.BAR_HEIGHT + 2)        # ease-out rounds to 0 early)
        self.assertFalse(g.closing)
        self.assertIsNone(g.out_img)                                  # ...and released
        self.assertEqual(gm.FADE_MS, overlay.ANIM_MS)

    def test_one_shape_and_the_open_icon(self):
        """No gap: the panel and the bar are one rounded shape with a hairline between;
        the gallery button wears the chosen-value look while the gallery is open."""
        bar = self.bar()
        rest = bar.gallery_btn.target()
        g = self.open(bar)
        pump(self.app, 0.05)
        img = bar.grab().toImage()
        y = bar.gallery_host.geometry().bottom() + 1                  # the join
        self.assertEqual(bar.gallery_join.geometry().top(), y)
        edge = QColor(img.pixel(3, y + 2)).alpha()                    # the left edge just under the join:
        self.assertGreater(edge, 200)                                 # surface, not a gap
        self.assertGreater(QColor(img.pixel(3, y - 3)).alpha(), 200)
        bar.setFocus()                                                # the icon without keyboard focus
        bar.gallery_btn.sync(animate=False)
        state, (fill, text, _ring) = bar.gallery_btn.target()
        self.assertEqual((state, fill.name(), text.name()),
                         ("selected", QColor(overlay.PILL_SEL).name(), QColor(overlay.ON_TEXT).name()))
        self.assertNotEqual(rest[1][0].name(), fill.name())
        self.shot(bar, "18-one-shape")
        self.key(Qt.Key_Escape)
        self.assertIn(bar.gallery_btn.target()[0], ("rest", "focus", "hover"))   # not "selected"
        del g

    def test_motion_open_and_close_order(self):
        """Open: the height grows first, the content fades in and slides up a moment later.
        Close: the content fades out first, the shape shrinks after; quicker than opening."""
        gm = self.motion()
        self.assertTrue(220 <= gm.OPEN_MS <= 260)
        self.assertEqual(gm.CLOSE_GROW_DELAY_MS + gm.CLOSE_GROW_MS, 180)   # closing: quicker
        self.assertLess(gm.CLOSE_FADE_MS, gm.CLOSE_GROW_DELAY_MS + gm.CLOSE_GROW_MS)
        bar = self.bar()
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        self.assertTrue(bar.gallery_btn.selected())                   # the highlight comes in at once
        pump(self.app, 0.035)
        self.assertGreater(g.reveal, 0.0)                             # growing...
        self.assertEqual(g.fade, 0.0)                                 # ...the content not yet
        host_h = bar.gallery_host.height()
        self.assertEqual(g.panel_w.y(), host_h - g.panel_height() + gm.CONTENT_SLIDE_PX)   # low, ready to slide
        self.wait_for(lambda: 0.2 < g.fade < 0.9, timeout=1)
        self.assertGreater(g.panel_w.y() - (bar.gallery_host.height() - g.panel_height()), 0)
        self.wait_for(lambda: g.fade == 1.0 and g.reveal == 1.0, timeout=1)
        pump(self.app, 0.02)
        self.assertEqual(g.panel_w.geometry().bottom(), bar.gallery_host.height() - 1)   # in place
        self.assertIsNone(g.panel_w.graphicsEffect())
        stage_size = g.stage.size()
        self.key(Qt.Key_Escape)                                       # close
        self.wait_for(lambda: g.fade < 1.0, timeout=0.5)              # the content goes first...
        self.assertGreater(g.reveal, 0.97)                            # ...the shape still whole
        self.assertEqual(g.stage.size(), stage_size)                  # never relaid out on the way
        self.wait_for(lambda: bar.gallery_host.isHidden(), timeout=1)

    def test_motion_reverses_from_where_it_is(self):
        self.motion()
        bar = self.bar()
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        self.wait_for(lambda: g.reveal > 0.4, timeout=1)
        self.key(Qt.Key_Escape)                                       # closing while still opening
        r0 = g.reveal
        pump(self.app, 0.03)
        self.assertLessEqual(g.reveal, r0 + 1e-6)                     # no jump back to open
        self.assertGreater(g.reveal, 0.0)                             # nor to closed
        self.assertTrue(g.closing)
        r1 = g.reveal
        self.key(Qt.Key_G)                                            # and opening again mid-way
        self.wait_for(lambda: bar.mode == "gallery", timeout=2)
        self.assertGreaterEqual(g.reveal, r1 - 0.2)                   # from there, not from 0
        self.wait_for(lambda: g.reveal == 1.0 and g.fade == 1.0, timeout=2)

    def test_motion_open_frames(self):
        """A strip of the open animation's frames, for review (gallery-open-frames.png)."""
        gm = self.motion()
        self.screen_size()
        bar = self.bar()
        self.key(Qt.Key_G)
        self.wait_for(lambda: bar.mode == "gallery")
        g = bar.gallery
        for tw in (g.reveal_tween, g.fade_tween):
            tw.stop()
        frames, heights = [], []
        ease = QEasingCurve(QEasingCurve.OutCubic)
        for ms in (0, 60, 120, 180, 240):
            g._reveal_tick(ease.valueForProgress(ms / gm.OPEN_MS))
            f = max(0.0, min(1.0, (ms - gm.OPEN_FADE_DELAY_MS) / gm.OPEN_FADE_MS))
            if g.panel_w.graphicsEffect() is None:
                from PySide6.QtWidgets import QGraphicsOpacityEffect
                g.panel_w.setGraphicsEffect(QGraphicsOpacityEffect(g.panel_w))
            g._fade_tick(ease.valueForProgress(f))
            pump(self.app, 0.02)
            heights.append(bar.height())
            frames.append(bar.grab())
        self.assertEqual(heights, sorted(heights))                    # only ever grows
        g._fade_done()
        full = max(f.height() for f in frames)
        scale = 0.5
        strip = QPixmap(int(sum(f.width() * scale + 16 for f in frames) + 16), int(full * scale + 32))
        strip.fill(QColor("#4a5563"))
        p = QPainter(strip)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        x = 16
        for f in frames:
            w, h = f.width() * scale, f.height() * scale
            p.drawPixmap(QRectF(x, 16 + full * scale - h, w, h).toRect(), f)
            x += w + 16
        p.end()
        strip.save(str(SHOT_DIR / "momento-gallery-open-frames.png"))

    def test_motion_crossfade_releases_the_outgoing_picture(self):
        self.motion()
        bar = self.bar()
        g = self.open(bar)
        self.settle_motion(g)
        self.key(Qt.Key_Right)                                        # to a screenshot
        self.assertIsNotNone(g.out_img)                               # the clip's last picture holds...
        self.assertTrue(g.xf_waiting)
        self.assertEqual(g.out_dir, 1)
        self.assertEqual((g.out_img.width(), g.out_img.height()), (g.stage.width(), g.stage.height()))
        self.wait_for(lambda: g.state == "shown", timeout=3)          # ...until the new one is read
        self.assertFalse(g.xf_waiting)
        self.wait_for(lambda: g.out_img is None, timeout=2)           # then fades out and is let go
        self.assertEqual(g.xf, 1.0)
        for _ in range(4):                                            # rapid browsing: never a queue
            self.key(Qt.Key_Right)
            pump(self.app, 0.03)
            self.assertLessEqual(sum(x is not None for x in (g.out_img,)), 1)
        self.assertEqual(g.current().path, self.clip2)
        self.wait_for(lambda: g.state == "playing" and g.out_img is None, timeout=3)

    def test_motion_icons_badge_highlight_and_knob(self):
        self.motion()
        bar = self.bar()
        g = self.open(bar)
        self.settle_motion(g)
        mute = g.panel.w["mute"]
        self.key(Qt.Key_M)                                            # sound on: the glyph crossfades
        self.assertEqual((mute.kind, mute.prev_kind), ("sound", "muted"))
        self.assertLess(g.badge_t, 1.0 + 1e-9)
        self.settle_motion(g)
        self.assertEqual((mute.prev_kind, mute.kt), (None, 1.0))
        self.assertEqual(g.badge_t, 0.0)                              # the muted badge faded out
        self.key(Qt.Key_M)
        self.settle_motion(g)
        self.assertEqual(g.badge_t, 1.0)
        g.set_row("filter")
        self.key(Qt.Key_Right)                                        # Clips: the highlight slides
        header = g.header
        self.assertIs(header.sel, g.tabs["clip"])
        self.assertIsNotNone(header.r0)
        self.settle_motion(g)
        self.assertEqual(header.rect_now(), header.pill(g.tabs["clip"]))
        scrub = g.panel.w["scrub"]
        self.key(Qt.Key_L)                                            # +10 s: the knob glides there
        self.settle_motion(g)
        self.assertAlmostEqual(scrub.value, 10 / 60, delta=0.02)
        self.key(Qt.Key_Space)                                        # pause / play: a glyph crossfade too
        self.assertEqual(g.panel.w["play"].prev_kind, "pause")

    def test_motion_full_screen_grows_and_the_strip_fades(self):
        self.motion()
        bar = self.bar()
        g = self.open(bar)
        self.settle_motion(g)
        self.key(Qt.Key_F)
        view = g.full
        self.assertLess(g.full_t, 1.0)                                # growing out of the stage
        self.assertFalse(view.strips["clip"].isVisible())
        self.wait_for(lambda: g.full_t == 1.0 and g.chrome, timeout=2)
        self.settle_motion(g)
        self.assertTrue(view.strips["clip"].isVisible())
        self.assertIsNone(view.strips["clip"].graphicsEffect())
        self.assertTrue(view.hasFocus())                              # full screen: the stage row
        g.chrome_timer.timeout.emit()                                 # 2.5 s untouched: it fades out
        self.assertTrue(g.chrome)
        self.settle_motion(g)
        self.assertFalse(g.chrome)
        self.assertFalse(view.strips["clip"].isVisible())
        self.key(Qt.Key_Escape)                                       # back: shrinks into the stage
        self.assertIsNone(g.full)
        self.assertIs(g.leaving, view)
        self.assertEqual(bar.mode, "gallery")
        self.wait_for(lambda: g.leaving is None, timeout=2)
        import shiboken6

        self.assertFalse(shiboken6.isValid(view) and view.isVisible())   # hidden, or already deleted

    def test_gallery_idle_and_leave_rules(self):
        from PySide6.QtCore import QEvent

        bar = self.bar()
        bar.resident = True
        bar.request_exit = lambda code: None
        g = self.open(bar)
        self.assertEqual(g.state, "playing")
        self.app.sendEvent(bar, QEvent(QEvent.Leave))                 # watching, pointer parked away
        self.assertFalse(bar.leave.isActive())
        self.assertFalse(bar.idle.isActive())
        self.key(Qt.Key_Space)                                        # paused: 10 s, not 3 s
        self.assertEqual(bar.idle.interval(), overlay.GALLERY_IDLE_MS)
        self.key(Qt.Key_F)                                            # full screen: exempt from leave
        self.app.sendEvent(bar, QEvent(QEvent.Leave))
        self.assertFalse(bar.leave.isActive())
        self.key(Qt.Key_F)
        pump(self.app, 0.1)                                           # (the bar, active again, gets an Enter)
        self.app.sendEvent(bar, QEvent(QEvent.Leave))                 # paused, in the bar: 0.5 s
        self.assertTrue(bar.leave.isActive())
        self.wait_for(lambda: not bar.isVisible(), timeout=2)
        self.assertIsNone(g.player)                                   # the normal teardown

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
        self.assertTrue(view.hasFocus())                              # the stage row; ↓ for the player
        self.assertEqual(g.fullc.w["full"].kind, "unfull")
        strip = view.strips["clip"]
        self.assertEqual(strip.width(), view.width() - 64)
        self.assertEqual(view.height() - strip.geometry().bottom() - 1, 32)
        self.full_shot(view, "07-fullscreen-clip")
        self.key(Qt.Key_Right)                                        # browsing works full screen
        self.settle_items(g)
        self.assertTrue(g.is_shot())
        self.assertTrue(view.strips["shot"].isVisible())
        self.assertTrue(view.hasFocus())                              # browsing: the stage row
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
        self.assertTrue(g.stage.hasFocus())                           # back on the stage row
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
        bar.on_pad_action("accept")                                   # A on the filter row: the stage
        self.assertEqual(g.row, "stage")
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
        bar = self.resident()
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
        self.assertEqual(self.exits, [])            # shown again at once: no recycle

    # ------------------------------------------------------------ recycle
    def resident(self, folder=None, daemon=None):
        bar = self.bar(folder, daemon=daemon)
        bar.resident = True
        self.exits = []
        bar.request_exit = self.exits.append        # instead of ending the test's event loop
        return bar

    def test_recycle_after_gallery_once_hidden(self):
        from momento import config

        bar = self.resident()
        self.open(bar)
        self.key(Qt.Key_Escape)                                       # back to the clip view
        pump(self.app, 0.05)
        self.assertEqual(self.exits, [])                              # never while shown
        bar.on_idle()                                                 # the idle hide
        pump(self.app, 0.05)
        self.assertEqual(self.exits, [config.BAR_RECYCLE_EXIT])

    def test_no_recycle_without_gallery_or_when_shown_again(self):
        bar = self.resident(self.empty)
        self.key(Qt.Key_G)                                            # nothing saved: no gallery
        self.wait_for(lambda: not bar.hintbar.isHidden())
        bar.dismiss()
        pump(self.app, 0.05)
        self.assertEqual(self.exits, [])
        bar.present()
        self.addCleanup(setattr, bar, "recycle", False)
        bar.recycle = True                                            # as after a gallery session
        bar.dismiss()
        bar.present()                                                 # the hotkey again, right away
        pump(self.app, 0.05)
        self.assertTrue(bar.isVisible())
        self.assertEqual(self.exits, [])                              # shown: stays

    def test_one_shot_bar_never_recycles(self):
        bar = self.bar()
        exits = []
        bar.request_exit = exits.append
        self.open(bar)
        bar.dismiss()
        pump(self.app, 0.05)
        self.assertEqual(exits, [])

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
            if shiboken6.isValid(o):                                  # (may be gone already)
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
