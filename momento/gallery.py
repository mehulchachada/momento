"""The clip bar's gallery: saved clips and screenshots, browsed and played in the bar.

The bar imports this module the first time its gallery opens (the gallery
button left of the free space, key G, or the controller), so a resident bar
that is never asked for it doesn't load QtMultimedia at all. Every open lists
the clips folder again (``momento.media.scan`` in a worker thread) and starts
on the newest item, muted, playing.

Layout: a panel of its own right above the bar, as wide as the bar, with the
bar row unchanged under it (one surface that grows upward like settings; the
bar paints the two apart). Top to bottom: the filters (All · Clips ·
Screenshots) with a ``‹ 3 / 42 ›`` counter, a 16:9 stage, the transport row
(−10, play/pause, +10, sound, time, scrubber, length, full screen; a
screenshot shows its size and format instead) and a footer (what it is and
when, the hints, Back). The hints show a controller's buttons while the bar has
one connected (they follow a hotplug), else the keys. They carry the symbols of
the controller in use (``Bar.pad_symbols``): ✕ ○ □ △ and L1 / R1 / L2 / R2 on a
PlayStation one, Nintendo's letters on a Nintendo one, Xbox letters otherwise,
and switch when another controller is picked up.

Clips play muted. The speaker button, M or X / Square (the west button) turns
the sound on; that sticks while the gallery is open (the next clip too, full
screen too) and every open starts muted again.

Playback is a QMediaPlayer feeding a QVideoSink; the frames are painted by the
stage itself (no QVideoWidget, which would be a separate native surface).
There is no audio output until the sound is turned on, a clip that ends stays
on its last frame, and everything (player, sink, audio, full screen view) is
torn down when the gallery closes or the bar hides: a hidden bar never plays.

Full screen is its own layer-shell surface on the Overlay layer, anchored to
every edge of the bar's screen and taking the keyboard; while it is up the bar
gives the keyboard up, so two exclusive-keyboard surfaces never coexist.
Without layer-shell it is a frameless, topmost full-screen window.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRectF, QSize,
                            Qt, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QImage, QImageReader, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import config, gamepad, media
from . import overlay as ov

log = logging.getLogger(__name__)

# Builds the media player: (parent) -> a QMediaPlayer. Tests swap in a fake with the
# same signals and methods; the gallery gives it a QVideoSink of its own.
PLAYER_FACTORY = None
# Builds the audio output, only once the sound is turned on: (parent) -> QAudioOutput.
AUDIO_FACTORY = None

STEP_MS = 120            # browsing: the next clip loads this long after the last step
SEEK_S = 10              # LT / RT, J / L
CHROME_MS = 2_500        # full screen: the strip hides after this long without input
FULL_MARGIN = 32         # full screen: the strip floats this far from the edges
STAGE_PAD = 16           # the stage's side margins inside the bar
STAGE_RADIUS = 8
META_PX = 13
MIN_STAGE_H = 160
CACHE_SIZE = 64          # clip lengths and screenshot sizes remembered (no thumbnails are kept)

# Motion, in the bar's own language: short ease-outs, nothing that queues. ANIMATE False
# (reduced motion; the tests) makes every transition land on its end state at once.
ANIMATE = True
FADE_MS = ov.ANIM_MS     # icons, the muted badge, the filter highlight, the panel, the strip
XFADE_MS = 160           # the stage, from one item to the next
SLIDE_PX = 10            # ...drifting this far in the direction of travel
XFADE_WAIT_MS = 400      # the old picture waits at most this long for the new one
SEEK_ANIM_MS = 120       # the scrubber's knob on a jump (±10 s, a click)
FULL_MS = 180            # into and out of full screen, from / to the stage
CLOCK_MS = 33            # while playing: the scrubber and time follow at ~30 Hz between updates
TRIM_DELAY_MS = 1_000    # after closing: give the heap back once the player is deleted

PLAY_ERROR = "Can't play this clip here"
SHOW_ERROR = "Can't show this screenshot"
FILTERS = (("all", "All"), ("clip", "Clips"), ("shot", "Screenshots"))
EMPTY_FILTER = {"all": "Nothing saved yet", "clip": "No clips yet", "shot": "No screenshots yet"}
KIND_NAMES = {"clip": "Clip", "shot": "Screenshot"}

# The hints in the footer and the full screen strip: [([buttons or keys], word), ...].
# A controller's buttons while the bar has one (Bar.pad_connected), else the keys.
# PAD_*: by button position (gamepad names); pad_hint() puts in the pad's own symbols.
PAD_CLIP = [(["tl", "tr"], "browse"), (["↑", "↓"], "filter"), (["south"], "play"), (["tl2", "tr2"], "10 s"),
            (["west"], "sound"), (["north"], "full screen")]
PAD_SHOT = [(["tl", "tr"], "browse"), (["↑", "↓"], "filter"), (["north"], "full screen")]
PAD_BACK = [(["east"], "Back")]
CLIP_KEYS = [(["←", "→"], "browse"), (["↑", "↓"], "filter"), (["Space"], "play"), (["J", "L"], "10 s"),
             (["M"], "sound"), (["F"], "full screen")]
SHOT_KEYS = [(["←", "→"], "browse"), (["↑", "↓"], "filter"), (["F"], "full screen")]
BACK_KEYS = [(["Esc"], "Back")]

_pad_hints: dict = {}


def pad_hint(tokens, symbols="xbox"):
    """``PAD_*`` hints with the buttons as a ``symbols`` pad labels them ("xbox",
    "playstation", "nintendo"). The same list object for the same input, so the
    footer repaints only on a real change."""
    key = (id(tokens), symbols)
    if key not in _pad_hints:
        _pad_hints[key] = [([gamepad.button_symbol(b, symbols) for b in btns], word) for btns, word in tokens]
    return _pad_hints[key]


CLIP_HINT, SHOT_HINT, BACK_HINT = (pad_hint(t) for t in (PAD_CLIP, PAD_SHOT, PAD_BACK))


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

def font(px=15, tabular=False, weight=QFont.Medium):
    """The bar's UI font at ``px`` pixels (tabular figures for times and counters)."""
    f = QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setStyleStrategy(QFont.NoSubpixelAntialias)   # grey text on a dark, translucent surface
    if tabular:
        try:
            f.setFeature(QFont.Tag("tnum"), 1)
        except Exception:  # noqa: BLE001 - Qt < 6.7
            pass
    return f


def when(mtime: float, now: float | None = None) -> str:
    """'Today 21:04', 'Yesterday 18:30', 'Sep 24 21:04', or 'Sep 24 2025' for an older year."""
    t = _dt.datetime.fromtimestamp(mtime)
    n = _dt.datetime.fromtimestamp(time.time() if now is None else now)
    hm = t.strftime("%H:%M")
    if t.date() == n.date():
        return f"Today {hm}"
    if t.date() == n.date() - _dt.timedelta(days=1):
        return f"Yesterday {hm}"
    if t.year == n.year:
        return f"{t:%b} {t.day} {hm}"
    return f"{t:%b} {t.day} {t.year}"


def size_label(n) -> str:
    """'4.0 MB', '812 KB', '1.2 GB'."""
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        v = n / 1e6
        return f"{v:.0f} MB" if v >= 100 else f"{v:.1f} MB"
    return f"{max(1.0, n / 1e3):.0f} KB"


def _sep():
    return "&nbsp;·&nbsp;"


def meta_html(kind: str, parts) -> str:
    """'<b>Clip</b> · 1:00 · Today 21:04': the kind bright, the rest muted."""
    rest = _sep().join(ov._esc(p) for p in parts if p)
    tail = f"<span style='color:{ov.MUTED}'>{_sep()}{rest}</span>" if rest else ""
    return f"<span style='color:{ov.TEXT}'>{ov._esc(kind)}</span>{tail}"


def dims_html(dims, size, suffix) -> str:
    """'1920×1080 · 4.0 MB · PNG' (the size bright, the rest muted)."""
    parts = []
    if dims is not None and dims.isValid():
        parts.append(f"<span style='color:{ov.TEXT}'>{dims.width()}×{dims.height()}</span>")
    parts.append(size_label(size))
    parts.append(ov._esc(suffix.lstrip(".").upper() or "Image"))
    return f"<span style='color:{ov.MUTED}'>{_sep().join(parts)}</span>"


# --------------------------------------------------------------------------
# glyphs (painted, ~16 px, like the bar's own)
# --------------------------------------------------------------------------

def draw_speaker(p, x, y, color, muted):
    """A filled speaker with sound waves, or a cross when muted."""
    p.save()
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    cx = x - 0.5
    p.drawPolygon(QPolygonF([QPointF(cx - 7, y - 2.6), QPointF(cx - 4.4, y - 2.6), QPointF(cx - 0.6, y - 6.4),
                             QPointF(cx - 0.6, y + 6.4), QPointF(cx - 4.4, y + 2.6), QPointF(cx - 7, y + 2.6)]))
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if muted:
        mx, s = x + 4.6, 2.6
        p.drawLine(QPointF(mx - s, y - s), QPointF(mx + s, y + s))
        p.drawLine(QPointF(mx - s, y + s), QPointF(mx + s, y - s))
    else:
        for r in (3.4, 6.4):
            p.drawArc(QRectF(cx - r, y - r, 2 * r, 2 * r), -48 * 16, 96 * 16)
    p.restore()


def draw_brackets(p, x, y, color, inward=False):
    """Four corner brackets: full screen (outward) or leave it (inward)."""
    pen = QPen(color, 1.7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.save()
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        if inward:
            cx, cy = x + sx * 2.6, y + sy * 2.2
            pts = [QPointF(cx, cy + sy * 3.6), QPointF(cx, cy), QPointF(cx + sx * 3.6, cy)]
        else:
            cx, cy = x + sx * 6.5, y + sy * 5.5
            pts = [QPointF(cx, cy - sy * 3.6), QPointF(cx, cy), QPointF(cx - sx * 3.6, cy)]
        p.drawPolyline(QPolygonF(pts))
    p.restore()


def draw_replay(p, x, y, color, scale=1.0):
    """A circular arrow: play the clip again from the start."""
    p.save()
    p.translate(x, y)
    p.scale(scale, scale)
    pen = QPen(color, 1.8)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    r = 5.6
    p.drawArc(QRectF(-r, -r + 0.6, 2 * r, 2 * r), 110 * 16, 290 * 16)   # open at the top left
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPolygon(QPolygonF([QPointF(-3.6, -7.6), QPointF(-3.4, -1.9), QPointF(1.0, -5.0)]))
    p.restore()


PS_GLYPHS = ("✕", "○", "□", "△")    # drawn, not typed: fonts render them unevenly


def ps_glyph(p, r, sym, color):
    """A PlayStation face symbol, a thin outline centred in ``r`` (the chip's text colour)."""
    c = r.center()
    pen = QPen(QColor(color), 1.5)
    pen.setJoinStyle(Qt.MiterJoin if sym == "□" else Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.save()
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if sym == "✕":
        d = 3.4
        p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
        p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
    elif sym == "○":
        p.drawEllipse(c, 4.0, 4.0)
    elif sym == "□":
        p.drawRect(QRectF(c.x() - 3.6, c.y() - 3.6, 7.2, 7.2))
    else:                               # △, its centre of mass on the chip's centre
        h = 7.4
        p.drawPolygon(QPolygonF([QPointF(c.x(), c.y() - h * 2 / 3), QPointF(c.x() + 4.3, c.y() + h / 3),
                                 QPointF(c.x() - 4.3, c.y() + h / 3)]))
    p.restore()


def chip_run(p, x, y, tokens, word_color=ov.MUTED):
    """Paint controller hints ([LB][RB] browse   [A] play ...) from (x, centre y);
    returns the width. ``p`` None only measures. ✕ ○ □ △ are drawn (``ps_glyph``)."""
    cf = font(10, weight=QFont.Bold)
    wf = font(12)
    cfm, wfm = QFontMetrics(cf), QFontMetrics(wf)
    start = x
    for gi, (btns, word) in enumerate(tokens):
        if gi:
            x += 16
        for bi, b in enumerate(btns):
            if bi:
                x += 3
            glyph = b in PS_GLYPHS
            w = 18 if glyph else max(18, cfm.horizontalAdvance(b) + 10)
            r = QRectF(x, y - 9, w, 18)
            if p is not None:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ov.TAB_SEL))
                rad = 9 if w == 18 else 5
                p.drawRoundedRect(r, rad, rad)
                if glyph:
                    ps_glyph(p, r, b, ov.PILL_SEL)
                else:
                    p.setPen(QColor(ov.PILL_SEL))
                    p.setFont(cf)
                    p.drawText(r, Qt.AlignCenter, b)
            x += w
        x += 6
        if p is not None:
            p.setPen(QColor(word_color))
            p.setFont(wf)
            p.drawText(QRectF(x, y - 10, wfm.horizontalAdvance(word) + 2, 20), Qt.AlignVCenter | Qt.AlignLeft, word)
        x += wfm.horizontalAdvance(word)
    return x - start


class _Tween:
    """A 0 -> 1 (or any start -> end) ease-out that restarts from where it is, never queues.
    With ANIMATE off, ``run`` lands on the end value (and calls ``on_done``) at once."""

    def __init__(self, parent, ms, on_value, on_done=None):
        self.on_value, self.on_done = on_value, on_done
        self.value = 1.0
        self.end = 1.0
        self.anim = QVariantAnimation(parent)
        self.anim.setDuration(ms)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._done)

    def running(self):
        return self.anim.state() == QAbstractAnimation.Running

    def run(self, start=0.0, end=1.0):
        self.anim.stop()
        self.end = float(end)
        if not ANIMATE or start == end:
            self._tick(end)
            self._done()
            return
        self.anim.setStartValue(float(start))
        self.anim.setEndValue(float(end))
        self.value = float(start)
        self.anim.start()

    def stop(self):
        self.anim.stop()

    def finish(self):
        """Jump to the end now (and call on_done) if it is running."""
        if self.running():
            self.anim.stop()
            self._tick(self.end)
            self._done()

    def _tick(self, v):
        self.value = float(v)
        self.on_value(self.value)

    def _done(self):
        if self.on_done is not None:
            self.on_done()


def _alpha(color, f):
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * f)))
    return c


def _lerp_rect(a, b, t):
    return QRectF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t,
                  a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)


def _no_video_frame():
    from PySide6.QtMultimedia import QVideoFrame

    return QVideoFrame()


def _trim_heap():
    """Hand the decoder's freed buffers back to the system (glibc keeps them otherwise):
    about 50 MB of a resident bar's memory after a 1080p clip."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _fit(iw, ih, r: QRectF) -> QRectF:
    """``iw`` x ``ih`` scaled to fit ``r``, centred (letterboxed)."""
    if iw <= 0 or ih <= 0:
        return QRectF(r)
    s = min(r.width() / iw, r.height() / ih)
    w, h = iw * s, ih * s
    return QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h)


def _label(text="", px=META_PX, color=ov.MUTED, tabular=False, width=None,
           align=Qt.AlignVCenter | Qt.AlignLeft):
    lb = QLabel(text)
    lb.setFont(font(px, tabular))
    lb.setStyleSheet(f"color: {color};")
    lb.setTextFormat(Qt.RichText)
    lb.setAlignment(align)
    if width:
        lb.setFixedWidth(width)
    return lb


# --------------------------------------------------------------------------
# widgets (subclasses of the bar's own pills, handed over in ``kit``)
# --------------------------------------------------------------------------

def _widgets(kit):
    cached = getattr(kit, "gallery_widgets", None)
    if cached is not None:
        return cached
    IconButton, TabButton = kit.IconButton, kit.TabButton

    class MediaIcon(IconButton):
        TIPS = {**IconButton.TIPS, "play": "Play (K)", "pause": "Pause (K)", "replay": "Play again (K)",
                "back10": "Back 10 s (J)", "fwd10": "Forward 10 s (L)", "muted": "Turn sound on (M)",
                "sound": "Mute (M)", "full": "Full screen (F)", "unfull": "Leave full screen (F)"}

        def __init__(self, kind, height=ov.BAR_HEIGHT):
            super().__init__(kind)
            self.setFixedSize(ov.ICON_W, height)
            self.prev_kind = None         # the glyph fading out (play -> pause, muted -> sound)
            self.kt = 1.0
            self.ktween = _Tween(self, FADE_MS, self._kt, self._kdone)

        def set_kind(self, kind):
            if kind == self.kind:
                return
            old = self.kind
            super().set_kind(kind)
            if ANIMATE and self.isVisible():
                self.prev_kind = old
                self.ktween.run()
            else:
                self.ktween.stop()
                self.prev_kind, self.kt = None, 1.0

        def _kt(self, v):
            self.kt = v
            self.update()

        def _kdone(self):
            self.prev_kind, self.kt = None, 1.0
            self.update()

        def paint_content(self, p, r, color):
            if self.prev_kind is not None and self.kt < 1.0:
                self.glyph(p, r, _alpha(color, 1.0 - self.kt), self.prev_kind)
                self.glyph(p, r, _alpha(color, self.kt), self.kind)
            else:
                self.glyph(p, r, color, self.kind)

        def glyph(self, p, r, color, k):
            c = r.center()
            x, y = c.x(), c.y()
            if k in ("back10", "fwd10"):
                p.setPen(color)
                p.setFont(font(12, True, QFont.DemiBold))
                p.drawText(r.adjusted(0, 0, 0, -1), Qt.AlignCenter, "−10" if k == "back10" else "+10")
            elif k in ("muted", "sound"):
                draw_speaker(p, x, y, color, k == "muted")
            elif k in ("full", "unfull"):
                draw_brackets(p, x, y, color, inward=k == "unfull")
            elif k == "replay":
                draw_replay(p, x, y, color)
            else:
                shown, self.kind = self.kind, k     # the bar's own glyphs (play, pause) paint self.kind
                p.save()
                super().paint_content(p, r, color)
                p.restore()
                self.kind = shown

    class FilterTab(TabButton):
        """A filter pill. The chosen one paints only its text: the header paints its fill,
        so the highlight can slide from one filter to the next."""

        def __init__(self, text):
            super().__init__(text)        # (target() sets self.hl: what the header paints under it)
            self.setAccessibleName(f"Show {text.lower()}")

        def target(self):
            state, style = super().target()
            if self.selected() and state != "disabled":
                self.hl = style
                return state, (QColor(0, 0, 0, 0), style[1], 0.0)
            self.hl = None
            return state, style

        def sync(self, animate=True):
            super().sync(animate)
            if self.parentWidget() is not None:
                self.parentWidget().update()

    class FilterHeader(QWidget):
        """The filters' row; paints the chosen filter's pill and slides it on a change."""

        def __init__(self):
            super().__init__()
            self.sel = None
            self.r0 = None                # where the slide started (header coordinates)
            self.t = 1.0
            self.tween = _Tween(self, FADE_MS, self._tick)

        def pill(self, tab):
            return tab.pill_rect().translated(QPointF(tab.pos()))

        def rect_now(self):
            if self.sel is None:
                return None
            to = self.pill(self.sel)
            return to if self.r0 is None or self.t >= 1.0 else _lerp_rect(self.r0, to, self.t)

        def select(self, tab, animate=True):
            if tab is self.sel:
                return
            start = self.rect_now()
            self.sel = tab
            if tab is None or start is None or not animate or not self.isVisible():
                self.tween.stop()
                self.r0, self.t = None, 1.0
                self.update()
                return
            self.r0 = start
            self.tween.run()

        def _tick(self, v):
            self.t = v
            self.update()

        def paintEvent(self, ev):
            tab = self.sel
            r = self.rect_now()
            if tab is None or r is None or tab.hl is None:
                return
            fill, _text, ring = tab.hl
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            if ring > 0.01:
                c = QColor(ov.RING)
                c.setAlphaF(min(1.0, ring))
                p.setPen(QPen(c, 1.5))
                p.setBrush(Qt.NoBrush)
                rr = r.adjusted(-2.75, -2.75, 2.75, 2.75)
                p.drawRoundedRect(rr, rr.height() / 2, rr.height() / 2)
            if fill.alpha():
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            p.end()

    class CounterPill(TabButton):
        """‹ 3 / 42 › as one tab-sized pill: the left half steps to newer, the right to older."""

        def __init__(self, text="0 / 0"):
            super().__init__(text)
            self.setFont(font(ov.TAB_PX, tabular=True))
            self.side = 1
            self.setAccessibleName("Browse")
            self.set_count(text)

        def set_count(self, text):
            if text != self.text() or not getattr(self, "_sized", False):
                self._sized = True
                self.setText(text)
                self.setFixedWidth(QFontMetrics(self.font()).horizontalAdvance(text)
                                   + 2 * (ov.TAB_PAD + ov.PILL_INSET) + 2 * 14)
                self.update()

        def target(self):
            state, style = super().target()
            if state == "rest":   # the counter reads as text, not as a quiet tab
                return state, (QColor(0, 0, 0, 0), QColor(ov.TEXT), 0.0)
            return state, style

        def mouseReleaseEvent(self, ev):
            self.side = -1 if ev.position().x() < self.width() / 2 else 1
            super().mouseReleaseEvent(ev)

        def paint_content(self, p, r, color):
            p.setPen(color)
            p.setFont(self.font())
            p.drawText(r, Qt.AlignCenter, self.text())
            pen = QPen(color if self.visual_state in ("hover", "focus") else QColor(ov.MUTED), 1.6)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            cy = r.center().y()
            for cx, d in ((r.left() + 14, -1), (r.right() - 14, 1)):
                p.drawPolyline(QPolygonF([QPointF(cx - d * 2, cy - 4), QPointF(cx + d * 2, cy),
                                          QPointF(cx - d * 2, cy + 4)]))

    class Stage(QWidget):
        """The picture: the current video frame or screenshot, letterboxed in a rounded frame."""

        clicked = Signal()
        double = Signal()

        def __init__(self, g, radius=STAGE_RADIUS):
            super().__init__()
            self.g = g
            self.radius = radius
            self.setCursor(Qt.PointingHandCursor)
            self.setAccessibleName("Clip")

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            self.g.paint_picture(p, QRectF(self.rect()), self.radius, badge=True)
            p.end()

        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                self.clicked.emit()

        def mouseDoubleClickEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                self.double.emit()

    class Scrubber(QWidget):
        """Where the clip is: a track, the part played, a knob. Click or drag to seek."""

        seek = Signal(float)

        def __init__(self, height=ov.ROW_H):
            super().__init__()
            self.value = 0.0
            self.v0 = self.v1 = 0.0
            self.tween = _Tween(self, SEEK_ANIM_MS, self._glide)
            self.setFixedHeight(height)
            self.setCursor(Qt.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.setAccessibleName("Position")

        def set_value(self, v, animate=False):
            """``animate``: glide there (a seek). A glide under way keeps gliding, to the new value."""
            v = max(0.0, min(1.0, float(v)))
            if self.tween.running():
                self.v1 = v
                return
            if animate and ANIMATE and self.isVisible() and abs(v - self.value) > 1e-3:
                self.v0, self.v1 = self.value, v
                self.tween.run()
                return
            if abs(v - self.value) > 1e-4:
                self.value = v
                self.update()

        def _glide(self, t):
            self.value = self.v0 + (self.v1 - self.v0) * t
            self.update()

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            y = self.height() / 2
            x0, x1 = 6, self.width() - 6
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#333333"))
            p.drawRoundedRect(QRectF(x0, y - 2, x1 - x0, 4), 2, 2)
            xp = x0 + (x1 - x0) * self.value
            if self.isEnabled():
                p.setBrush(QColor(ov.TEXT))
                p.drawRoundedRect(QRectF(x0, y - 2, xp - x0, 4), 2, 2)
                p.setBrush(QColor(ov.PILL_ON))
                p.drawEllipse(QPointF(xp, y), 6, 6)
            p.end()

        def _frac(self, ev):
            return (ev.position().x() - 6) / max(1.0, self.width() - 12)

        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton and self.isEnabled():
                self.seek.emit(max(0.0, min(1.0, self._frac(ev))))

        def mouseMoveEvent(self, ev):
            if ev.buttons() & Qt.LeftButton and self.isEnabled():
                self.seek.emit(max(0.0, min(1.0, self._frac(ev))))

    class Chips(QWidget):
        """Hints on their own (full screen: [B] / [Esc] Back); a click does what they say."""

        clicked = Signal()

        def __init__(self, tokens, height=ov.BAR_HEIGHT):
            super().__init__()
            self.tokens = None
            self.setFixedHeight(height)
            self.set_tokens(tokens)
            self.setCursor(Qt.PointingHandCursor)

        def set_tokens(self, tokens):
            if tokens is not self.tokens:
                self.tokens = tokens
                self.setFixedWidth(int(chip_run(None, 0, 0, tokens)) + 10)
                self.update()

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            chip_run(p, 0, self.height() / 2, self.tokens)
            p.end()

        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                self.clicked.emit()

    class Footer(QWidget):
        """The panel's last row: what and when · controller or key hints (centred) · Back."""

        def __init__(self):
            super().__init__()
            self.hint = CLIP_KEYS
            self.setFixedHeight(ov.BAR_HEIGHT)
            lay = QHBoxLayout(self)
            lay.setContentsMargins(18, 0, 0, 0)
            lay.setSpacing(0)
            self.meta = _label("", META_PX)
            self.meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(self.meta, 1)
            self.back = kit.TextButton("Back", glyph="back", quiet=True)
            lay.addWidget(self.back)
            lay.addSpacing(8)

        def set_hint(self, tokens):
            if tokens is not self.hint:
                self.hint = tokens
                self.update()

        def paintEvent(self, ev):
            if not self.hint:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w = chip_run(None, 0, 0, self.hint)
            chip_run(p, self.width() / 2 - w / 2, self.height() / 2, self.hint)
            p.end()

    class Surface(QWidget):
        """The bar's surface (#111 at ~94 %, 1 px border, 10 px corners) for the full screen strip."""

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(ov.BORDER), 1))
            p.setBrush(QColor(*ov.BG))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
            p.end()

    class FullView(QWidget):
        """Full screen: the picture edge to edge and a floating strip 32 px above the bottom."""

        def __init__(self, g):
            super().__init__()
            self.g = g
            self.setWindowTitle("Momento gallery")
            self.setFocusPolicy(Qt.StrongFocus)
            self.setMouseTracking(True)
            self.setAutoFillBackground(False)
            self.setStyleSheet(f"QWidget {{ color: {ov.TEXT}; background: transparent; }}"
                               "QPushButton { border: none; outline: none; }")
            self.setAttribute(Qt.WA_TranslucentBackground)   # it fades in over the game
            self.strips = {"clip": g.build_strip(self, "clip"), "shot": g.build_strip(self, "shot")}

        @property
        def focus_visible(self):          # read by the pills: the ring follows the bar's rule
            return self.g.bar.focus_visible

        def strip(self):
            return self.strips["shot" if self.g.is_shot() else "clip"]

        def place(self):
            for kind, s in getattr(self, "strips", {}).items():
                s.setVisible(self.g.chrome and kind == ("shot" if self.g.is_shot() else "clip"))
                if kind == "clip":
                    w = max(200, self.width() - 2 * FULL_MARGIN)
                else:
                    s.layout().activate()
                    w = min(self.width() - 2 * FULL_MARGIN, s.layout().sizeHint().width())
                s.setFixedWidth(int(w))
                s.move(int((self.width() - w) / 2), self.height() - FULL_MARGIN - s.height())

        def resizeEvent(self, ev):
            self.place()
            super().resizeEvent(ev)

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            g = self.g
            t = g.full_t
            full = QRectF(self.rect())
            p.fillRect(full, QColor(0, 0, 0, int(255 * t)))
            if t < 1.0 and g.full_from is not None:   # growing out of (or back into) the stage
                g.paint_picture(p, _lerp_rect(g.full_from, full, t), STAGE_RADIUS * (1.0 - t), badge=False)
            else:
                g.paint_picture(p, full, 0, badge=False)
            p.end()

        def mouseMoveEvent(self, ev):
            self.g.wake_chrome()
            super().mouseMoveEvent(ev)

        def mousePressEvent(self, ev):
            self.g.wake_chrome()
            if ev.button() == Qt.LeftButton and not self.g.is_shot():
                self.g.toggle_play()

        def mouseDoubleClickEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                if not self.g.is_shot():
                    self.g.toggle_play()      # undo the first click's play / pause
                self.g.toggle_full()

    ns = type("GalleryWidgets", (), {})()
    for c in (MediaIcon, FilterTab, FilterHeader, CounterPill, Stage, Scrubber, Chips, Footer, Surface, FullView):
        setattr(ns, c.__name__, c)
    kit.gallery_widgets = ns
    return ns


class _Controls:
    """The transport's widgets in one view (the bar's panel, or the full screen strip)."""

    def __init__(self):
        self.w = {}          # name -> widget: play back10 fwd10 mute full counter now total scrub meta dims
        self.clipbox = None  # the clip-only part of the panel's row
        self.shotbox = None  # the screenshot-only part


# --------------------------------------------------------------------------
# the gallery
# --------------------------------------------------------------------------

class Gallery(QObject):
    """One per bar. ``open()`` lists the folder and shows it; ``close()`` tears it all down."""

    scanned = Signal(int, object)        # (bar gen, [MediaItem] or None)
    loaded = Signal(int, object)         # (load token, (QImage, QSize, path))
    probed = Signal(object, object)      # ((path, mtime), seconds or None)

    def __init__(self, bar, kit):
        super().__init__(bar)
        self.bar, self.kit = bar, kit
        self.W = _widgets(kit)
        self.active = False       # the gallery is showing
        self.scanning = False
        self.items = []           # everything, newest first
        self.view = []            # the filtered list
        self.filter = "all"
        self.index = 0
        self.last_mtime = None    # where we were, for a filter that had nothing
        self.token = 0            # bumped on every item change and image load
        self.state = "idle"       # idle | empty | loading | playing | paused | ended | error | shown
        self.frame = None         # QImage on the stage
        self.message = None       # text on the stage instead of a picture
        self.position = 0.0       # seconds
        self.duration = 0.0
        self.muted = True
        self.player = self.sink = self.audio = None
        self.durations = {}       # (path, mtime) -> seconds (None: unknown); LRU, CACHE_SIZE
        self.probing = set()
        self.dims = {}            # path -> QSize of a screenshot; LRU, CACHE_SIZE
        self.full = None          # FullView while full screen
        self.full_layered = False
        self.chrome = True        # full screen: the strip is showing
        self.panel = _Controls()
        self.fullc = None         # _Controls of the full screen strip
        # motion
        self.reveal = 0.0         # the panel: 0 folded away .. 1 open (the bar grows with it)
        self.closing = False      # folding away after Back (the clip view is already live)
        self.out_img = None       # the outgoing picture during a crossfade (one, at the stage's size)
        self.out_dir = 0          # -1 newer / +1 older: which way it drifts
        self.xf = 1.0
        self.xf_waiting = False   # the old picture holds until the new one is there
        self.badge_t = 1.0        # the muted badge's opacity
        self.full_t = 1.0         # full screen: 0 at the stage .. 1 edge to edge
        self.full_from = None     # the stage's rect in the full screen view, for the grow
        self.leaving = None       # a full screen view on its way out
        self.pos_at = 0.0         # when self.position was last reported (the clock interpolates)

        self.step_timer = QTimer(self)
        self.step_timer.setSingleShot(True)
        self.step_timer.setInterval(STEP_MS)
        self.step_timer.timeout.connect(self._load)
        self.chrome_timer = QTimer(self)
        self.chrome_timer.setSingleShot(True)
        self.chrome_timer.setInterval(CHROME_MS)
        self.chrome_timer.timeout.connect(self._hide_chrome)
        self.trim_timer = QTimer(self)      # the heap back to the system once a decoder is gone
        self.trim_timer.setSingleShot(True)
        self.trim_timer.setInterval(TRIM_DELAY_MS)
        self.trim_timer.timeout.connect(_trim_heap)
        self.reveal_tween = _Tween(self, FADE_MS, self._reveal_tick, self._reveal_done)
        self.xf_tween = _Tween(self, XFADE_MS, self._xf_tick, self._xf_done)
        self.xf_wait = QTimer(self)
        self.xf_wait.setSingleShot(True)
        self.xf_wait.setInterval(XFADE_WAIT_MS)
        self.xf_wait.timeout.connect(self._xf_go)
        self.badge_tween = _Tween(self, FADE_MS, self._badge_tick)
        self.full_tween = _Tween(self, FULL_MS, self._full_tick, self._full_done)
        self.chrome_tween = _Tween(self, FADE_MS, self._chrome_tick, self._chrome_done)
        self.clock = QTimer(self)             # only while a clip plays
        self.clock.setInterval(CLOCK_MS)
        self.clock.timeout.connect(self._tick_clock)
        self.scanned.connect(self._on_scanned)
        self.loaded.connect(self._on_loaded)
        self.probed.connect(self._on_probed)
        self._build_panel()

    # ------------------------------------------------------------------ building
    @property
    def stage_size(self):
        """16:9 across the panel (1006 x 566), smaller only when the screen is too short for
        it (a 1280x720 desktop at 150 %): the bar with its gallery always fits on screen."""
        w = self.bar.bar_w - 2 - 2 * STAGE_PAD
        h = round(w * 9 / 16)
        screen = self.bar.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            rest = (ov.BAR_HEIGHT + 2 + ov.GALLERY_GAP + 2          # the bar, the gap, the edges
                    + ov.PANEL_PAD_T + ov.TABS_H + 4 + ov.ROW_PITCH + 1 + ov.BAR_HEIGHT)
            room = screen.availableGeometry().height() - 2 * ov.BOTTOM_MARGIN - rest
            if room < h:
                h = max(MIN_STAGE_H, room)
                w = min(w, round(h * 16 / 9))
        return QSize(w, h)

    def panel_height(self):
        return ov.PANEL_PAD_T + ov.TABS_H + 4 + self.stage.height() + ov.ROW_PITCH + 1 + ov.BAR_HEIGHT

    def shown_height(self):
        """The panel's height right now (it grows / folds with ``reveal``)."""
        return int(round(self.panel_height() * max(0.0, min(1.0, self.reveal))))

    def eventFilter(self, obj, ev):
        if obj is self.bar.gallery_host and ev.type() in (QEvent.Resize, QEvent.Show):
            self._pin_panel()
        return False

    def _pin_panel(self):
        host = self.bar.gallery_host
        h = self.panel_height()
        self.panel_w.setFixedHeight(h)
        self.panel_w.setGeometry(0, host.height() - h, host.width(), h)

    def _build_panel(self):
        W = self.W
        c = self.panel
        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, ov.PANEL_PAD_T, 0, 0)
        pl.setSpacing(0)

        header = self.header = W.FilterHeader()
        header.setFixedHeight(ov.TABS_H)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 12, 0)
        hl.setSpacing(0)
        self.tabs = {}
        for key, name in FILTERS:
            b = W.FilterTab(name)
            b.clicked.connect(lambda _=False, key=key: self.set_filter(key, focus="filter"))
            hl.addWidget(b)
            self.tabs[key] = b
        hl.addStretch(1)
        c.w["counter"] = W.CounterPill()
        c.w["counter"].clicked.connect(lambda: self.step(c.w["counter"].side, focus="counter"))
        hl.addWidget(c.w["counter"])
        pl.addWidget(header)

        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(STAGE_PAD, 4, STAGE_PAD, 0)
        sl.setSpacing(0)
        self.stage = W.Stage(self)
        self.stage.setFixedSize(self.stage_size)
        self.stage.clicked.connect(self._stage_clicked)
        self.stage.double.connect(self._stage_double)
        sl.addWidget(self.stage, 0, Qt.AlignHCenter)
        pl.addWidget(sw)

        row = QWidget()
        row.setFixedHeight(ov.ROW_PITCH)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(12, 2, 12, 0)
        rl.setSpacing(0)
        c.clipbox = QWidget()
        cl = QHBoxLayout(c.clipbox)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self._transport(c, cl, ov.ROW_H)
        rl.addWidget(c.clipbox, 1)
        c.shotbox = QWidget()
        xl = QHBoxLayout(c.shotbox)
        xl.setContentsMargins(6, 0, 0, 0)
        xl.setSpacing(0)
        c.w["dims"] = _label("", META_PX, ov.MUTED, True)
        xl.addWidget(c.w["dims"])
        xl.addStretch(1)
        rl.addWidget(c.shotbox, 1)
        c.shotbox.hide()
        c.w["full"] = W.MediaIcon("full", ov.ROW_H)
        c.w["full"].clicked.connect(lambda: self.toggle_full())
        rl.addWidget(c.w["full"])
        pl.addWidget(row)

        line = QFrame()                   # the hairline over the footer, like the bar's own
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {ov.BORDER}; margin: 0 12px;")
        pl.addWidget(line)
        self.footer = W.Footer()
        c.w["meta"] = self.footer.meta
        c.w["back"] = self.footer.back
        self.footer.back.clicked.connect(self.back)
        pl.addWidget(self.footer)
        # Not in the host's layout: pinned to the host's bottom edge, so while the host grows
        # (or folds) the panel rises up from (or sinks back to) the bar instead of squeezing.
        self.panel_w = panel
        panel.setParent(self.bar.gallery_host)
        panel.setFixedHeight(self.panel_height())
        self.bar.gallery_host.installEventFilter(self)

    def _transport(self, c, lay, height):
        """−10 · play · +10 · sound · time · scrubber · length (into ``lay``)."""
        W = self.W
        for name, kind, fn in (("back10", "back10", lambda: self.seek(-SEEK_S, focus="back10")),
                               ("play", "play", lambda: self.toggle_play(focus="play")),
                               ("fwd10", "fwd10", lambda: self.seek(SEEK_S, focus="fwd10")),
                               ("mute", "muted", lambda: self.toggle_mute(focus="mute"))):
            b = W.MediaIcon(kind, height)
            b.clicked.connect(fn)
            c.w[name] = b
            lay.addWidget(b)
        lay.addSpacing(10)
        tw = QFontMetrics(font(META_PX, True)).horizontalAdvance("00:00")
        c.w["now"] = _label("0:00", META_PX, ov.TEXT, True, tw, Qt.AlignVCenter | Qt.AlignRight)
        lay.addWidget(c.w["now"])
        lay.addSpacing(8)
        c.w["scrub"] = W.Scrubber(height)
        c.w["scrub"].seek.connect(self.seek_to)
        lay.addWidget(c.w["scrub"], 1)
        lay.addSpacing(8)
        c.w["total"] = _label("0:00", META_PX, ov.MUTED, True, tw)
        lay.addWidget(c.w["total"])
        lay.addSpacing(6)

    def build_strip(self, parent, kind):
        """The full screen strip for a clip (the whole transport) or a screenshot (compact)."""
        W = self.W
        if self.fullc is None:
            self.fullc = _Controls()
        c = self.fullc
        strip = W.Surface(parent)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(13, 1, 9, 1)
        lay.setSpacing(0)

        def divider():
            d = self.kit.divider()
            return d
        if kind == "clip":
            self._transport(c, lay, ov.BAR_HEIGHT)
            c.w["full"] = W.MediaIcon("unfull")
            c.w["full"].clicked.connect(lambda: self.toggle_full())
            lay.addWidget(c.w["full"])
            lay.addSpacing(10)
            lay.addWidget(divider())
            lay.addSpacing(16)
            c.w["meta"] = _label("", META_PX)
            lay.addWidget(c.w["meta"])
        else:
            c.w["counter"] = W.CounterPill()
            c.w["counter"].setFixedHeight(ov.BAR_HEIGHT)
            c.w["counter"].clicked.connect(lambda: self.step(c.w["counter"].side, focus="counter"))
            lay.addWidget(c.w["counter"])
            lay.addSpacing(8)
            lay.addWidget(divider())
            lay.addSpacing(16)
            c.w["shotmeta"] = _label("", META_PX)
            lay.addWidget(c.w["shotmeta"])
            lay.addSpacing(16)
            c.w["dims"] = _label("", META_PX, ov.MUTED, True)
            lay.addWidget(c.w["dims"])
            lay.addSpacing(12)
            c.w["shotfull"] = W.MediaIcon("unfull")
            c.w["shotfull"].clicked.connect(lambda: self.toggle_full())
            lay.addWidget(c.w["shotfull"])
        lay.addSpacing(10)
        lay.addWidget(divider())
        lay.addSpacing(16)
        back = W.Chips(pad_hint(PAD_BACK, self.bar.pad_symbols()) if self.bar.pad_connected() else BACK_KEYS)
        back.clicked.connect(self.back)
        lay.addWidget(back)
        strip.setFixedHeight(ov.BAR_HEIGHT + 2)
        return strip

    # ------------------------------------------------------------------ state
    def current(self):
        return self.view[self.index] if 0 <= self.index < len(self.view) else None

    def is_shot(self):
        item = self.current()
        return item is not None and item.kind == "shot"

    def playing(self):
        return self.active and self.state == "playing"

    def window_pills(self):
        """The full screen view's pills (the bar restyles them with its own)."""
        if self.full is None:
            return []
        return [b for b in self.full.findChildren(QPushButton) if isinstance(b, self.kit.Pill)]

    # ------------------------------------------------------------------ open / close
    def open(self):
        """List the clips folder in a worker; the gallery shows once it has an answer."""
        if self.active or self.scanning:
            return
        self.scanning = True
        gen = self.bar.gen
        st = self.bar.last_status or {}
        out = st.get("output_dir") if st.get("ok") else None

        def work():
            try:
                folder = out or config.load()["output"]["dir"]
                items = media.scan(folder)
            except Exception:  # noqa: BLE001 - a broken config: nothing to show
                log.exception("cannot list the clips folder")
                items = []
            self.scanned.emit(gen, items)
        threading.Thread(target=work, name="momento-gallery-scan", daemon=True).start()

    def _on_scanned(self, gen, items):
        self.scanning = False
        bar = self.bar
        if (gen != bar.gen or not bar.isVisible() or bar.mode != "clip" or bar.saving or bar.done
                or bar.control_busy):
            return
        if not items:
            bar.gallery_empty()
            return
        self.active = True
        self.items = list(items)
        self.filter = "all"
        self.view = list(self.items)
        self.index = 0
        self.muted = True           # every open starts muted
        self.badge_tween.stop()
        self.badge_t = 1.0
        self.chrome = True
        self.stage.setFixedSize(self.stage_size)   # the screen may have changed since
        self.header.select(self.tabs[self.filter], animate=False)
        self._xf_drop()
        start = self.reveal if self.closing else 0.0   # reopened while folding: from there
        self.closing = False
        self.reveal_tween.stop()
        self.reveal = start
        bar.enter_gallery()
        self._show(immediate=True)
        self.focus_default()
        self._unfold(start, 1.0)

    def close(self, fold=False):
        """Tear everything down: player, sink, audio, full screen; forget the listing.
        ``fold``: the panel folds away (Back), holding a still of the stage meanwhile;
        otherwise (the bar hides) it is gone at once."""
        self.scanning = False
        if not fold:
            self._stop_motion()
        if not self.active and self.player is None and self.full is None:
            return
        self.active = False
        self.token += 1
        self.step_timer.stop()
        self.clock.stop()
        self.exit_full(restore=False)
        played = self.player is not None
        self._release_frame()
        self._drop_player()
        self.durations.clear()
        self.dims.clear()
        self.probing.clear()
        if played:
            self.trim_timer.start()   # once the player's objects are gone
        self.items, self.view = [], []
        self.index = 0
        self.frame = None
        self.message = None
        self.state = "idle"
        self.position = self.duration = 0.0

    def back(self):
        """B / Esc / Backspace: full screen -> the gallery -> the clip view."""
        if self.full is not None:
            self.exit_full()
            return
        if ANIMATE:
            still = self._snapshot(self.stage)   # what the stage shows, while the panel folds
            self.close(fold=True)
            self._xf_drop()
            self.out_img, self.xf_waiting = still, still is not None
            self.closing = True
        else:
            self.close()
        self.bar.leave_gallery()
        self._unfold(self.reveal, 0.0)

    # ------------------------------------------------------------------ motion
    def _stop_motion(self):
        """Every transition to its end, no animation left running (the bar hides)."""
        for tw in (self.reveal_tween, self.xf_tween, self.badge_tween, self.chrome_tween, self.full_tween):
            tw.stop()
        self.xf_wait.stop()
        self.closing = False
        self.reveal = 0.0
        self.panel_w.setGraphicsEffect(None)
        self._xf_drop()
        self._finish_leaving()

    def _unfold(self, start, end):
        """The panel grows up out of the bar row (end 1) or folds back into it (end 0),
        its content fading with it."""
        if ANIMATE and start != end:
            effect = QGraphicsOpacityEffect(self.panel_w)
            effect.setOpacity(start)
            self.panel_w.setGraphicsEffect(effect)   # only while it moves (it costs a buffer)
        self.reveal_tween.run(start, end)

    def _reveal_tick(self, v):
        self.reveal = v
        effect = self.panel_w.graphicsEffect()
        if effect is not None:
            effect.setOpacity(v)
        self.bar.relayout()

    def _reveal_done(self):
        self.panel_w.setGraphicsEffect(None)
        if self.reveal <= 0.0:
            self.closing = False
            self._xf_drop()
        self.bar.relayout()

    def _snapshot(self, widget):
        """What ``widget`` (the stage or the full screen view) shows now, as one image of its
        size; None when it shows no picture."""
        if widget is None or (self.frame is None and self.out_img is None) or widget.width() <= 0:
            return None
        dpr = widget.devicePixelRatioF() or 1.0
        img = QImage(int(widget.width() * dpr), int(widget.height() * dpr), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        self._paint_layers(p, QRectF(0, 0, widget.width(), widget.height()))
        p.end()
        return img

    def _begin_switch(self, d):
        """Another item: the stage's picture now becomes the outgoing one, held until the
        new picture is there, then crossfaded with a small drift (``d``: -1 newer, +1 older)."""
        if not ANIMATE or not self.active:
            return
        still = self._snapshot(self.full if self.full is not None else self.stage)
        self.xf_tween.stop()
        self.xf_wait.stop()
        self.out_img, self.out_dir, self.xf = still, d, 0.0   # the previous outgoing one goes
        self.xf_waiting = still is not None
        if self.xf_waiting:
            self.xf_wait.start()

    def _xf_go(self):
        """The new picture is there (or took too long): crossfade to it."""
        if not self.xf_waiting:
            return
        self.xf_wait.stop()
        self.xf_waiting = False
        self.xf_tween.run()

    def _xf_tick(self, v):
        self.xf = v
        self._repaint()

    def _xf_done(self):
        self.out_img = None       # released as soon as it is faded out
        self.xf = 1.0
        self._repaint()

    def _xf_drop(self):
        self.xf_tween.stop()
        self.xf_wait.stop()
        self.out_img, self.xf_waiting, self.xf = None, False, 1.0

    def _badge_tick(self, v):
        self.badge_t = v
        self.stage.update()

    def _tick_clock(self):
        """While playing: the time and knob move between the player's own updates."""
        if not self.active or self.state != "playing" or self.is_shot():
            self.clock.stop()
            return
        est = self.position + (time.monotonic() - self.pos_at)
        if self.duration:
            est = min(est, self.duration)
        self.sync_time(est)

    # ------------------------------------------------------------------ items
    def _show(self, immediate=False):
        """Put the current item on the stage (a clip loads STEP_MS after the last step)."""
        self.token += 1
        self.step_timer.stop()
        item = self.current()
        if self.player is not None:
            self.player.stop()      # the old clip stops now, not when the next one loads
            if item is None or item.kind != "clip":
                self.player.setSource(QUrl())   # a screenshot: the decoder and its buffers go too
                self.trim_timer.start()
        self._release_frame()
        self.message = None
        self.position = 0.0
        self.duration = 0.0
        if item is None:
            self.state = "empty"
            self.message = EMPTY_FILTER.get(self.filter, EMPTY_FILTER["all"])
            self._xf_drop()
        else:
            self.last_mtime = item.mtime
            self.state = "loading"
            if item.kind == "clip":
                self.duration = self.durations.get((str(item.path), item.mtime)) or 0.0
                self._probe(item)
        self.sync()
        if item is not None:
            if immediate:
                self._load()
            else:
                self.step_timer.start()

    def _load(self):
        item = self.current()
        if item is None or not self.active:
            return
        if item.kind != "clip":
            self._load_image(item)
            return
        player = self._ensure_player()
        if player is None:
            self._fail(PLAY_ERROR)
            return
        try:
            player.setSource(QUrl.fromLocalFile(str(item.path)))
            player.play()
        except Exception:  # noqa: BLE001
            log.exception("cannot play %s", item.path)
            self._fail(PLAY_ERROR)

    def _fail(self, text):
        self.state = "error"
        self.message = text
        self.frame = None
        self._xf_drop()
        self.sync()

    def step(self, d, focus="counter"):
        """LB / RB, ← / →: ``d`` -1 = newer, +1 = older (no wrap)."""
        if not self.view:
            return
        i = max(0, min(len(self.view) - 1, self.index + d))
        if i != self.index:
            self._begin_switch(1 if i > self.index else -1)
            self.index = i
            self._show()
        self.focus(focus)

    def jump(self, i, focus="counter"):
        """Home / End."""
        if not self.view:
            return
        i = max(0, min(len(self.view) - 1, i))
        if i != self.index:
            self._begin_switch(1 if i > self.index else -1)
            self.index = i
            self._show()
        self.focus(focus)

    def set_filter(self, key, focus="filter"):
        """All / Clips / Screenshots, keeping your place in time (media.nearest)."""
        if key == self.filter:
            self.focus(focus)
            return
        old = self.current()
        mtime = old.mtime if old is not None else self.last_mtime
        self.filter = key
        self.view = media.filter_items(self.items, key)
        self.index = max(0, media.nearest(self.view, mtime)) if mtime is not None and self.view else 0
        new = self.current()
        if new is not None and old is not None and new.path == old.path:
            self.sync()             # the same item: it keeps playing
        else:
            self._begin_switch(0)   # a crossfade, no drift
            self._show(immediate=old is None)
        self.focus(focus)

    def step_filter(self, d):
        keys = [k for k, _n in FILTERS]
        i = max(0, min(len(keys) - 1, keys.index(self.filter) + d))
        self.set_filter(keys[i], focus="filter")

    # ------------------------------------------------------------------ playback
    def _ensure_player(self):
        if self.player is not None:
            return self.player
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
        except ImportError:
            log.warning("QtMultimedia is missing: the gallery cannot play clips")
            return None
        try:
            player = PLAYER_FACTORY(self) if PLAYER_FACTORY else QMediaPlayer(self)
            sink = QVideoSink(self)
            player.setVideoSink(sink)
        except Exception:  # noqa: BLE001
            log.exception("cannot create the media player")
            return None
        self.player, self.sink = player, sink
        self._States = QMediaPlayer.PlaybackState
        self._Status = QMediaPlayer.MediaStatus
        self._NoError = QMediaPlayer.Error.NoError
        sink.videoFrameChanged.connect(self._on_frame)
        player.positionChanged.connect(self._on_position)
        player.durationChanged.connect(self._on_duration)
        player.playbackStateChanged.connect(self._on_state)
        player.mediaStatusChanged.connect(self._on_status)
        player.errorOccurred.connect(self._on_error)
        self._apply_audio()
        return player

    def _drop_player(self):
        player, sink, audio = self.player, self.sink, self.audio
        self.player = self.sink = self.audio = None
        if player is not None:
            try:
                player.stop()
                player.setAudioOutput(None)
                player.setVideoSink(None)
                player.setSource(QUrl())
            except Exception:  # noqa: BLE001
                log.debug("player teardown", exc_info=True)
            player.deleteLater()
        for obj in (sink, audio):
            if obj is not None:
                obj.deleteLater()

    def _apply_audio(self):
        """No audio output at all while muted; one is made when the sound is turned on."""
        player = self.player
        if player is None:
            return
        if self.muted:
            if self.audio is not None:
                player.setAudioOutput(None)
                self.audio.deleteLater()
                self.audio = None
            return
        if self.audio is None:
            if AUDIO_FACTORY is not None:
                self.audio = AUDIO_FACTORY(self)
            else:
                from PySide6.QtMultimedia import QAudioOutput

                self.audio = QAudioOutput(self)
            player.setAudioOutput(self.audio)

    def _on_frame(self, frame):
        if not self.active or self.is_shot() or self.state == "error":
            return
        if frame is None or not frame.isValid():
            return
        # Keep the frame itself, no copy: it is converted when (and only if) it is painted,
        # and Qt caches that conversion in the frame, so at most one picture is alive
        # (two for the 160 ms of a crossfade).
        self.frame = frame
        if self.xf_waiting:
            self._xf_go()
        self._repaint()

    def _release_frame(self):
        """Let go of the picture on the stage, including the sink's own last video frame."""
        self.frame = None
        if self.sink is not None:
            self.sink.setVideoFrame(_no_video_frame())   # an invalid frame: ignored by _on_frame

    def _on_position(self, ms):
        if not self.active or self.is_shot():
            return
        self.position = max(0.0, ms / 1000.0)
        self.pos_at = time.monotonic()
        self.sync_time()

    def _on_duration(self, ms):
        if not self.active:
            return
        item = self.current()
        if ms and ms > 0 and item is not None and item.kind == "clip":
            self.duration = ms / 1000.0
            self._remember(self.durations, (str(item.path), item.mtime), self.duration)
            self.sync()

    def _on_state(self, state):
        if not self.active or self.is_shot() or self.state in ("error", "empty"):
            return
        if state == self._States.PlayingState:
            self.state = "playing"
            self.pos_at = time.monotonic()
            self.clock.start()
        elif state == self._States.PausedState:
            self.state = "paused"
        elif self.state not in ("ended", "loading"):
            self.state = "paused"
        self.sync()
        self._idle_changed()

    def _on_status(self, status):
        if not self.active or self.is_shot():
            return
        if status == self._Status.EndOfMedia:
            self.state = "ended"      # stays on the last frame
            if self.duration:
                self.position = self.duration
            self.sync()
            self._idle_changed()
        elif status == self._Status.InvalidMedia:
            self._fail(PLAY_ERROR)
            self._idle_changed()

    def _on_error(self, error, text=""):
        if not self.active or self.is_shot() or error == self._NoError:
            return
        log.warning("cannot play %s: %s", getattr(self.current(), "path", "?"), text)
        self._fail(PLAY_ERROR)
        self._idle_changed()

    def _idle_changed(self):
        self.bar.touch_idle()        # stops the auto-hide while playing, restarts it otherwise
        if self.full is not None and self.chrome:
            self.wake_chrome()

    def toggle_play(self, focus="play"):
        """A / Space / K / Enter: play or pause; again from the start once it ended.
        On a screenshot it opens (or leaves) full screen."""
        item = self.current()
        if item is None:
            return
        if item.kind == "shot":
            self.toggle_full()
            return
        if self.state == "error":
            return
        player = self.player
        if player is None or self.step_timer.isActive():
            self.step_timer.stop()
            self._load()
        elif self.state == "ended":
            player.setPosition(0)
            self.position = 0.0
            player.play()
        elif self.state in ("playing", "loading"):
            player.pause()
            self.state = "paused"
        else:
            player.play()
        self.sync()
        self.focus(focus)
        self._idle_changed()

    def seek(self, delta, focus=None):
        """LT / RT, J / L: ``delta`` seconds, clamped to the clip."""
        item = self.current()
        player = self.player
        if item is None or item.kind != "clip" or player is None or self.state in ("error", "loading"):
            if focus:
                self.focus(focus)
            return
        dur = self.duration or max(0.0, (player.duration() or 0) / 1000.0)
        self._seek_abs(self.position + delta, dur)
        if focus:
            self.focus(focus)

    def seek_to(self, frac):
        """A click / drag on the scrubber."""
        player = self.player
        if player is None or self.is_shot() or self.state in ("error", "loading"):
            return
        dur = self.duration or max(0.0, (player.duration() or 0) / 1000.0)
        if dur > 0:
            self._seek_abs(frac * dur, dur)

    def _seek_abs(self, t, dur):
        player = self.player
        t = max(0.0, min(dur, t)) if dur > 0 else max(0.0, t)
        player.setPosition(int(round(t * 1000)))
        self.position = t
        self.pos_at = time.monotonic()
        if self.state == "ended" and (dur <= 0 or t < dur):
            player.play()           # stepping back from the end plays that part again
        self.sync_time(animate=True)
        self.sync()

    def toggle_mute(self, focus="mute"):
        """X / Square (west) / M / the speaker button: sound on or off (off on every open;
        kept across items and in full screen)."""
        if self.is_shot() or self.current() is None:
            return
        self.muted = not self.muted
        self._apply_audio()
        self.badge_tween.run(self.badge_t, 1.0 if self.muted else 0.0)
        self.sync()
        self.focus(focus)

    # ------------------------------------------------------------------ screenshots
    def _load_image(self, item, size=None):
        """Read a screenshot scaled to the stage (or the screen) x the device pixel ratio."""
        self.token += 1
        token = self.token
        if size is None:
            dpr = self.bar.devicePixelRatioF() or 1.0
            target = self.full.size() if self.full is not None else self.stage.size()
            size = QSize(int(target.width() * dpr), int(target.height() * dpr))
        path = str(item.path)

        def work():
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            full = reader.size()
            if full.isValid() and size.isValid():
                s = full.scaled(size, Qt.KeepAspectRatio)
                if s.width() < full.width():
                    reader.setScaledSize(s)
            img = reader.read()
            self.loaded.emit(token, (img, full, path))
        threading.Thread(target=work, name="momento-gallery-image", daemon=True).start()

    def _on_loaded(self, token, result):
        if token != self.token or not self.active:
            return
        img, full, path = result
        item = self.current()
        if item is None or str(item.path) != path:
            return
        if full.isValid():
            self._remember(self.dims, path, full)
        if img.isNull():
            self._fail(SHOW_ERROR)
            return
        self.frame = img
        self.state = "shown"
        if self.xf_waiting:
            self._xf_go()
        self.sync()

    @staticmethod
    def _remember(cache, key, value):
        """A small LRU: the newest CACHE_SIZE entries stay."""
        cache.pop(key, None)
        cache[key] = value
        while len(cache) > CACHE_SIZE:
            cache.pop(next(iter(cache)))

    def _probe(self, item):
        """A clip's length for the footer, before the player knows it (ffprobe, in a worker)."""
        key = (str(item.path), item.mtime)
        if key in self.durations or key in self.probing:
            return
        self.probing.add(key)

        def work():
            try:
                secs = media.clip_duration(item.path)
            except Exception:  # noqa: BLE001
                secs = None
            self.probed.emit(key, secs)
        threading.Thread(target=work, name="momento-gallery-probe", daemon=True).start()

    def _on_probed(self, key, secs):
        self.probing.discard(key)
        if key not in self.durations:
            self._remember(self.durations, key, secs)
        item = self.current()
        if self.active and item is not None and (str(item.path), item.mtime) == key and not self.duration and secs:
            self.duration = float(secs)
            self.sync()

    # ------------------------------------------------------------------ painting
    def paint_picture(self, p, r, radius, badge):
        """The stage: the frame letterboxed on black, or a message; badges on top."""
        clip = QPainterPath()
        clip.addRoundedRect(r, radius, radius)
        p.save()
        p.setClipPath(clip)
        p.fillRect(r, QColor("#000000"))
        shown = self._paint_layers(p, r)
        if not shown and self.message:
            p.setPen(QColor(ov.MUTED))
            p.setFont(font(15))
            p.drawText(r, Qt.AlignCenter, self.message)
        p.restore()
        item = self.current()
        if item is None or item.kind != "clip" or self.state == "error":
            return
        if badge and self.badge_t > 0.01:
            b = QRectF(r.right() - 12 - 34, r.top() + 12, 34, 26)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, int(150 * self.badge_t)))
            p.drawRoundedRect(b, 13, 13)
            draw_speaker(p, b.center().x() - 0.5, b.center().y(), QColor(255, 255, 255, int(225 * self.badge_t)),
                         True)
        if self.state == "ended":
            c = r.center()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawEllipse(c, 28, 28)
            draw_replay(p, c.x(), c.y(), QColor(255, 255, 255, 235), scale=2.0)

    def _picture(self):
        img = self.frame
        if img is not None and not isinstance(img, QImage):
            img = img.toImage()      # a QVideoFrame (cached by Qt for that frame)
        return img if img is not None and not img.isNull() else None

    def _paint_layers(self, p, r):
        """The picture(s) in ``r``: the current one, and during a switch the outgoing one
        fading and drifting out while the new one fades and drifts in. True if any."""
        cur = self._picture()
        out = self.out_img
        if out is None:
            if cur is not None:
                p.drawImage(_fit(cur.width(), cur.height(), r), cur)
            return cur is not None
        t = 0.0 if self.xf_waiting else self.xf
        p.save()
        p.setOpacity(1.0 - t)
        p.drawImage(r.translated(-self.out_dir * SLIDE_PX * t, 0), out)
        if cur is not None and not self.xf_waiting:
            p.setOpacity(t)
            p.drawImage(_fit(cur.width(), cur.height(), r).translated(self.out_dir * SLIDE_PX * (1.0 - t), 0), cur)
        p.restore()
        return True

    def _repaint(self):
        if self.full is not None:
            self.full.update()
        else:
            self.stage.update()

    # ------------------------------------------------------------------ syncing the widgets
    def _views(self):
        return [c for c in (self.panel, self.fullc if self.full is not None else None) if c is not None]

    def sync_time(self, pos=None, animate=False):
        dur = self.duration
        pos = self.position if pos is None else pos
        for c in self._views():
            w = c.w
            if "now" in w:
                txt = ov._mmss(pos)
                if w["now"].text() != txt:
                    w["now"].setText(txt)
                w["scrub"].set_value(pos / dur if dur > 0 else 0.0, animate)

    def sync(self):
        item = self.current()
        clip = item is not None and item.kind == "clip"
        shot = item is not None and item.kind == "shot"
        n = len(self.view)
        count = f"{self.index + 1} / {n}" if n else "0 / 0"
        for key, b in self.tabs.items():
            b.set_sel(key == self.filter)
        self.header.select(self.tabs.get(self.filter))
        play_kind = "replay" if self.state == "ended" else "pause" if self.state in ("playing", "loading") else "play"
        usable = clip and self.state != "error"
        if item is not None:
            if clip:
                dur = self.duration or self.durations.get((str(item.path), item.mtime))
                meta = meta_html(KIND_NAMES["clip"], [ov._mmss(dur) if dur else None, when(item.mtime)])
            else:
                meta = meta_html(KIND_NAMES["shot"], [when(item.mtime)])
            dims = dims_html(self.dims.get(str(item.path)), item.size, item.path.suffix) if shot else ""
        else:
            meta, dims = "", ""
        self.sync_hints()
        for c in self._views():
            w = c.w
            if "counter" in w:
                w["counter"].set_count(count)
                w["counter"].setEnabled(n > 1)
            if "play" in w:
                w["play"].set_kind(play_kind)
                for k in ("play", "back10", "fwd10", "mute"):
                    w[k].setEnabled(usable)
                w["mute"].set_kind("muted" if self.muted else "sound")
                w["scrub"].setEnabled(usable)
                w["total"].setText(ov._mmss(self.duration) if self.duration else "0:00")
            if "full" in w:
                w["full"].setEnabled(item is not None and self.state != "error")
            if "meta" in w and w["meta"].text() != meta:
                w["meta"].setText(meta)
            if "shotmeta" in w:
                w["shotmeta"].setText(meta)
            if "dims" in w and w["dims"].text() != dims:
                w["dims"].setText(dims)
        if self.panel.clipbox is not None:
            self.panel.clipbox.setHidden(shot)
            self.panel.shotbox.setHidden(not shot)
        if self.full is not None:
            self.full.place()
        self.sync_time()
        self._repaint()
        self._fix_focus()

    def sync_hints(self):
        """The footer's hints and the full screen Back chip: a controller's buttons (with
        the symbols of the one in use) while the bar has one connected, else the keys
        (also called on a hotplug, and when another controller is picked up)."""
        pad = self.bar.pad_connected()
        sym = self.bar.pad_symbols() if pad else "xbox"
        item = self.current()
        if item is None:
            hint = []
        elif item.kind == "clip":
            hint = pad_hint(PAD_CLIP, sym) if pad else CLIP_KEYS
        else:
            hint = pad_hint(PAD_SHOT, sym) if pad else SHOT_KEYS
        self.footer.set_hint(hint)
        if self.full is not None:
            for chips in self.full.findChildren(self.W.Chips):
                chips.set_tokens(pad_hint(PAD_BACK, sym) if pad else BACK_KEYS)
            self.full.place()

    # ------------------------------------------------------------------ focus
    def _widget(self, name):
        """The control called ``name`` in the view that has the keyboard."""
        if name == "filter":
            if self.full is not None:
                return self._widget("counter" if self.is_shot() else "play")
            return self.tabs.get(self.filter)
        if self.full is not None and self.fullc is not None:
            if self.is_shot():
                name = {"full": "shotfull", "play": "shotfull", "counter": "counter"}.get(name, "shotfull")
            elif name == "counter":
                name = "play"
            return self.fullc.w.get(name)
        if self.is_shot() and name not in ("counter", "full", "back"):
            name = "full"
        return self.panel.w.get(name)

    def focus(self, name):
        if name is None:
            return
        w = self._widget(name)
        if w is None or not w.isVisible() or not w.isEnabled():
            self.focus_default()
            return
        w.setFocus(Qt.TabFocusReason)

    def focus_default(self):
        """The play button on a clip, full screen on a screenshot, else the counter / Back."""
        for name in ("full", "play") if self.is_shot() else ("play", "counter", "full", "back"):
            w = self._widget(name)
            if w is not None and w.isVisible() and w.isEnabled():
                w.setFocus(Qt.TabFocusReason)
                return
        if self.full is not None:
            self.full.setFocus(Qt.OtherFocusReason)
        else:
            self.footer.back.setFocus(Qt.OtherFocusReason)

    def _fix_focus(self):
        """A control that just hid or went disabled hands focus to the default one."""
        if not self.active:
            return
        w = QApplication.focusWidget()
        host = self.full if self.full is not None else self.bar
        if w is None or not host.isAncestorOf(w) or not w.isVisible() or not w.isEnabled():
            if host.isVisible():
                self.focus_default()

    # ------------------------------------------------------------------ full screen
    def toggle_full(self):
        if self.full is not None:
            self.exit_full()
        else:
            self.enter_full()

    def enter_full(self):
        if self.full is not None or self.current() is None or not self.active:
            return
        self._finish_leaving()
        bar = self.bar
        view = self.W.FullView(self)
        screen = bar.screen() or QGuiApplication.primaryScreen()
        layered = False
        if bar.layered:
            try:
                view.winId()
                if screen is not None and view.windowHandle() is not None:
                    view.windowHandle().setScreen(screen)
                    view.setGeometry(screen.geometry())
                layered = bool(self.kit.layer_full(view, screen))
            except Exception as e:  # noqa: BLE001
                log.warning("full screen as a layer surface failed (%s); using a window", e)
        if not layered:
            view.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            view.winId()
            if screen is not None:
                if view.windowHandle() is not None:
                    view.windowHandle().setScreen(screen)
                view.setGeometry(screen.geometry())
        self.full, self.full_layered = view, layered
        self.chrome = False       # the strip fades in once the picture has grown
        self.full_from = self._stage_rect(view)
        self.full_t = 0.0 if ANIMATE else 1.0
        view.place()
        if layered:
            self.kit.set_keyboard(bar, False)    # one surface with the keyboard at a time
            view.show()
        else:
            view.showFullScreen()
        view.raise_()
        view.activateWindow()
        self._activate(view)
        self.sync()
        for b in self.window_pills():
            b.sync(animate=False)
        view.setFocus(Qt.OtherFocusReason)
        self.full_tween.run(self.full_t, 1.0)
        if self.is_shot():
            self._load_image(self.current())    # sharp at the screen's size

    def _stage_rect(self, view):
        """The stage's rect in ``view``'s coordinates (where the picture grows from / shrinks to)."""
        try:
            bar = self.bar
            if bar.layered:
                # a layer surface doesn't know its position: the bar is bottom-centred
                scr = (bar.screen() or QGuiApplication.primaryScreen()).geometry()
                origin = QPoint(scr.x() + (scr.width() - bar.width()) // 2,
                                scr.y() + scr.height() - ov.BOTTOM_MARGIN - bar.height())
                top_left = origin + self.stage.mapTo(bar, QPoint(0, 0)) - scr.topLeft()
            else:
                top_left = self.stage.mapToGlobal(QPoint(0, 0)) - view.geometry().topLeft()
            return QRectF(QPointF(top_left), QSize(self.stage.width(), self.stage.height()).toSizeF())
        except Exception:  # noqa: BLE001 - only a nicer start for the animation
            return None

    def _full_tick(self, v):
        self.full_t = v
        view = self.full or self.leaving
        if view is not None:
            view.update()

    def _full_done(self):
        if self.leaving is not None:
            self._finish_leaving()
        elif self.full is not None:
            self.full_from = None
            self.wake_chrome()        # the strip fades in; focus goes to its default control

    def exit_full(self, restore=True):
        view = self.full
        if view is None:
            return
        self.full = None
        self.fullc = None
        self.chrome_timer.stop()
        self.chrome_tween.stop()
        for s in view.strips.values():
            s.setGraphicsEffect(None)
            s.hide()
        if restore and ANIMATE:
            # shrink back into the stage; the view goes (and the bar gets the keyboard) after
            self.full_tween.stop()
            self.leaving = view
            self.full_from = self._stage_rect(view)
            self.stage.update()
            self.full_tween.run(self.full_t, 0.0)
            self._back_to_panel()
            return
        self.full_tween.stop()
        view.hide()
        view.deleteLater()
        if not restore:
            return
        self._back_to_panel(view_gone=True)

    def _finish_leaving(self):
        view, self.leaving = self.leaving, None
        if view is None:
            return
        self.full_tween.stop()
        view.hide()
        view.deleteLater()
        if self.full_layered and self.bar.isVisible():
            self.kit.set_keyboard(self.bar, True)
            self.bar.activateWindow()
            self._activate(self.bar)

    def _back_to_panel(self, view_gone=False):
        bar = self.bar
        if view_gone and self.full_layered and bar.isVisible():
            self.kit.set_keyboard(bar, True)
        bar.raise_()
        bar.activateWindow()
        self._activate(bar)
        self.sync()
        self.focus("full")           # back where full screen was asked for
        item = self.current()
        if item is not None and item.kind == "shot":
            self._load_image(item)   # back to the stage's size: the screen-sized picture goes

    def _activate(self, window):
        """Make ``window`` Qt's active window now (the offscreen platform and some
        compositors only do it on the next round trip)."""
        handle = window.windowHandle()
        if QApplication.activeWindow() is not window and handle is not None:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                QApplication.setActiveWindow(window)

    def wake_chrome(self):
        """Full screen: show the strip; it hides again after CHROME_MS while nothing is paused."""
        if self.full is None:
            return
        if not self.chrome and self.full_t >= 1.0:
            self.chrome = True
            self.full.unsetCursor()
            self.full.place()
            self._fade_strip(1.0)
            self.focus_default()
        elif self.chrome and self.chrome_tween.running() and self.chrome_tween.end <= 0.0:
            self.full.unsetCursor()   # input while it fades out: back in
            self._fade_strip(1.0)
        if self.state in ("playing", "shown", "loading"):
            self.chrome_timer.start()
        else:
            self.chrome_timer.stop()

    def _hide_chrome(self):
        view = self.full
        if view is None or not self.chrome:
            return
        strip = view.strip()
        if strip.underMouse() or self.state in ("paused", "ended", "error"):
            return
        view.setFocus(Qt.OtherFocusReason)   # keys keep working with the strip gone
        view.setCursor(Qt.BlankCursor)
        self._fade_strip(0.0)

    def _fade_strip(self, end):
        """The full screen strip fades in (end 1) or out (end 0, then it hides)."""
        view = self.full
        if view is None:
            return
        strip = view.strip()
        effect = strip.graphicsEffect()
        start = effect.opacity() if effect is not None else (0.0 if end else 1.0)
        if ANIMATE:
            effect = QGraphicsOpacityEffect(strip)
            effect.setOpacity(start)
            strip.setGraphicsEffect(effect)
        self.chrome_tween.run(start, end)

    def _chrome_tick(self, v):
        view = self.full
        if view is not None:
            effect = view.strip().graphicsEffect()
            if effect is not None:
                effect.setOpacity(v)

    def _chrome_done(self):
        view = self.full
        if view is None:
            return
        view.strip().setGraphicsEffect(None)
        if self.chrome_tween.end <= 0.0:
            self.chrome = False
            view.place()

    # ------------------------------------------------------------------ input
    def _stage_clicked(self):
        self.bar.set_focus_visible(False)
        if self.is_shot():
            self.toggle_full()
        else:
            self.toggle_play()

    def _stage_double(self):
        if self.is_shot():
            return                   # the first click already opened full screen
        self.toggle_play()           # undo the first click's play / pause
        self.enter_full()

    def key(self, k) -> bool:
        """Keys while the gallery is open (the bar routes every key here)."""
        if self.full is not None:
            self.wake_chrome()
        if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
            self.back()
        elif k == Qt.Key_G and self.full is None:
            self.back()                            # G toggles, like it opened
        elif k in (Qt.Key_Left, Qt.Key_PageUp):
            self.step(-1)
        elif k in (Qt.Key_Right, Qt.Key_PageDown):
            self.step(1)
        elif k == Qt.Key_Home:
            self.jump(0)
        elif k == Qt.Key_End:
            self.jump(len(self.view) - 1)
        elif k in (Qt.Key_Up, Qt.Key_Down):
            self.step_filter(-1 if k == Qt.Key_Up else 1)
        elif k in (Qt.Key_Space, Qt.Key_K, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Select, Qt.Key_MediaTogglePlayPause):
            self.toggle_play(focus="full" if self.is_shot() else "play")
        elif k == Qt.Key_J:
            self.seek(-SEEK_S, focus="back10")
        elif k == Qt.Key_L:
            self.seek(SEEK_S, focus="fwd10")
        elif k == Qt.Key_M:
            self.toggle_mute()
        elif k == Qt.Key_F:
            self.toggle_full()
        return True                                # nothing else reaches the clip bar

    def pad(self, action):
        """Controller actions while the gallery is open."""
        if self.full is not None:
            self.wake_chrome()
        if action in ("prev_section", "left"):
            self.step(-1)
        elif action in ("next_section", "right"):
            self.step(1)
        elif action in ("up", "down"):
            self.step_filter(-1 if action == "up" else 1)
        elif action == "accept":
            self.toggle_play(focus="full" if self.is_shot() else "play")
        elif action == "back":
            self.back()
        elif action in ("left_trigger", "right_trigger"):
            self.seek(-SEEK_S if action == "left_trigger" else SEEK_S,
                      focus="back10" if action == "left_trigger" else "fwd10")
        elif action == "pause":                    # X / Square
            self.toggle_mute()
        elif action == "settings":                 # Y / Triangle
            self.toggle_full()
