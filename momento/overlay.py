"""The replay picker: a slim bar at the bottom of the screen.

Launched by the hotkey (or ``momento overlay``). Running it while another
bar is open closes the open one instead, so the same key toggles it.

Besides the clip lengths the bar has three painted glyph buttons: settings
(gear, key S), pause/resume (key P) and stop (asks inline first). Settings
open in the same bar, which grows upward into a few segmented rows; applying
goes through the daemon's ``configure`` IPC, or straight to the config file
when the daemon is off.

On KDE/wlroots Wayland the bar is a wlr-layer-shell surface on the Overlay
layer (drawn above fullscreen games), anchored to the bottom edge and sized to
the bar itself so clicks elsewhere still reach the game. LayerShellQt has no
Python bindings, so it is driven through ctypes. Anywhere that fails, the bar
is a frameless always-on-top tool window at the bottom centre of the screen.
"""

from __future__ import annotations

import ctypes
import html
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import RUNTIME_DIR
from .durations import PRESETS, label as dur_label

log = logging.getLogger(__name__)

PIDFILE = RUNTIME_DIR / "overlay.pid"
LAST_FILE = RUNTIME_DIR / "overlay.last"
LAYER_SHELL_PLUGIN = "wayland-shell-integration/liblayer-shell.so"
LAYER_SHELL_LIB = "libLayerShellQtInterface.so.6"

BOTTOM_MARGIN = 24
BAR_HEIGHT = 52
OPTION_MIN_WIDTH = 56
IDLE_CLOSE_MS = 10_000
RESULT_CLOSE_MS = 1_500
DEFAULT_SECONDS = 60
ICON_W = 36              # gear / pause / stop buttons
HINT_H = 30              # the "Paused" line above the bar
HEADER_H = 34            # settings: title line
ROW_H = 36               # settings: one row
PANEL_PAD_T = 6
PANEL_PAD_B = 6
LABEL_W = 96             # settings: row label column
SEG_PAD = 10
SEG_SPACING = 2
ARROW_W = 30
CYCLE_OVER = 4           # more devices than this -> ‹ current › instead of a row of names
SETTINGS_IDLE_MS = 30_000
START_TIMEOUT_S = 10
START_POLL_S = 0.5

# Palette. The record dot is the only accent colour.
BG = (17, 17, 17, 240)   # #111111 at ~94 %
BORDER = "#2A2A2A"
TEXT = "#EDEDED"
MUTED = "#8A8A8A"
DIM = "#555555"
RED = "#FF4D2E"
SEL_BG = "#262626"       # the chosen value in a settings row


# --------------------------------------------------------------------------
# toggle / pidfile / last choice
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    # Guard against a recycled pid belonging to something unrelated.
    return b"momento" in cmd or b"overlay" in cmd


def _toggle_existing() -> bool:
    """If another bar is running, SIGTERM it and return True."""
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return False
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False
    return False


def _write_pidfile():
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PIDFILE.with_suffix(".tmp")
        tmp.write_text(str(os.getpid()))
        os.replace(tmp, PIDFILE)
    except OSError as e:
        log.debug("cannot write pidfile: %s", e)


def _remove_pidfile():
    try:
        if PIDFILE.read_text().strip() == str(os.getpid()):
            PIDFILE.unlink()
    except OSError:
        pass


def _last_choice() -> int:
    try:
        secs = int(LAST_FILE.read_text().strip())
        if any(secs == s for s, _ in PRESETS):
            return secs
    except (OSError, ValueError):
        pass
    return DEFAULT_SECONDS


def _store_choice(seconds: int):
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LAST_FILE.write_text(str(seconds))
    except OSError:
        pass


def _mmss(seconds: float) -> str:
    """Minutes:seconds without rolling over to hours (60:00, not 1:00:00)."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _esc(text) -> str:
    return html.escape(str(text))


# --------------------------------------------------------------------------
# layer-shell
# --------------------------------------------------------------------------

def _is_wayland() -> bool:
    plat = os.environ.get("QT_QPA_PLATFORM", "")
    if plat and not plat.startswith("wayland"):
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


def _layer_shell_available() -> bool:
    if os.environ.get("MOMENTO_OVERLAY_NO_LAYERSHELL"):
        return False
    if not _is_wayland():
        return False
    try:
        from PySide6.QtCore import QLibraryInfo
        plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    except Exception:  # noqa: BLE001
        plugins = Path("/usr/lib64/qt6/plugins")
    candidates = [plugins / LAYER_SHELL_PLUGIN, Path("/usr/lib64/qt6/plugins") / LAYER_SHELL_PLUGIN,
                  Path("/usr/lib/qt6/plugins") / LAYER_SHELL_PLUGIN]
    if not any(p.exists() for p in candidates):
        return False
    try:
        ctypes.CDLL(LAYER_SHELL_LIB)
    except OSError:
        return False
    return True


def _apply_layer_shell(widget) -> bool:
    """Make ``widget``'s window a bottom-anchored Overlay-layer surface.

    Must run after the QWindow exists (winId()) and before the first show().
    The surface size comes from the widget size; with only the bottom edge
    anchored the compositor centres it horizontally.
    """
    import shiboken6

    lib = ctypes.CDLL(LAYER_SHELL_LIB)
    handle = widget.windowHandle()
    if handle is None:
        return False
    # QWindow inherits QObject and QSurface -> one pointer per base; QObject first.
    qwindow_ptr = shiboken6.getCppPointer(handle)[0]

    get = lib._ZN12LayerShellQt6Window3getEP7QWindow
    get.restype = ctypes.c_void_p
    get.argtypes = [ctypes.c_void_p]
    lsw = get(ctypes.c_void_p(qwindow_ptr))
    if not lsw:
        return False
    this = ctypes.c_void_p(lsw)

    def call(sym, value, argtype=ctypes.c_int):
        fn = getattr(lib, sym)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, argtype]
        fn(this, value)

    call("_ZN12LayerShellQt6Window8setLayerENS0_5LayerE", 3)                        # Overlay
    call("_ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE", 2)           # Bottom only
    call("_ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE", 1)  # Exclusive
    try:
        # setMargins(const QMargins&); QMargins is {int left, top, right, bottom}.
        margins = (ctypes.c_int * 4)(0, 0, 0, BOTTOM_MARGIN)
        call("_ZN12LayerShellQt6Window10setMarginsERK8QMargins", ctypes.addressof(margins), ctypes.c_void_p)
    except AttributeError:
        pass
    try:  # scope via Qt property (avoids building a QString by hand)
        from PySide6.QtCore import QObject
        shiboken6.wrapInstance(lsw, QObject).setProperty("scope", "momento-overlay")
    except Exception:  # noqa: BLE001
        pass
    return True


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def start_daemon() -> str:
    """Start the recorder: through its systemd user unit if installed, else detached."""
    try:
        unit = subprocess.run(["systemctl", "--user", "cat", "momento.service"],
                              capture_output=True, timeout=5)
        if unit.returncode == 0:
            subprocess.run(["systemctl", "--user", "start", "--no-block", "momento.service"],
                           capture_output=True, timeout=10)
            return "systemd"
    except (OSError, subprocess.SubprocessError):
        pass
    subprocess.Popen([sys.executable, "-m", "momento", "daemon"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    return "process"


def _gear_path(cx: float, cy: float, r_out=8.0, r_in=6.0, r_hole=2.6, teeth=8):
    """A crisp gear outline as a QPainterPath (no icon font needed)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath, QPolygonF

    step = 2 * math.pi / teeth
    top, base = step * 0.2, step * 0.3  # half-widths of a tooth at its tip / its root
    pts = []
    for i in range(teeth):
        a = i * step - math.pi / 2
        for ang, r in ((a - base, r_in), (a - top, r_out), (a + top, r_out), (a + base, r_in)):
            pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
        mid = a + step / 2  # keep the body round between teeth
        pts.append(QPointF(cx + r_in * math.cos(mid), cy + r_in * math.sin(mid)))
    path = QPainterPath()
    path.addPolygon(QPolygonF(pts))
    path.closeSubpath()
    path.addEllipse(QPointF(cx, cy), r_hole, r_hole)
    path.setFillRule(Qt.OddEvenFill)
    return path


RES_LABELS = {"720p": "720p", "1080p": "1080p", "1440p": "1440p", "2160p": "4K", "native": "Native"}


def _build(argv=None):  # noqa: C901 - one cohesive UI builder
    from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
    from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
                                   QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

    from . import config, ipc, quality, settings

    class Bridge(QObject):
        status = Signal(object)
        saved = Signal(object)
        settings = Signal(object)
        configured = Signal(object)
        control = Signal(str, object)
        started = Signal(object)

    def fetch_status(timeout=2.0):
        try:
            return ipc.request({"cmd": "status"}, timeout=timeout)
        except Exception as e:  # DaemonNotRunning or anything else
            return {"ok": False, "not_running": isinstance(e, getattr(ipc, "DaemonNotRunning", ())),
                    "error": str(e)}

    def local_settings():
        try:
            data = settings.describe(config.load())
        except Exception as e:  # noqa: BLE001 - e.g. a hand-broken config file
            return {"ok": False, "error": f"cannot read settings: {e}"}
        data["online"] = False
        return data

    def fetch_settings():
        try:
            r = ipc.request({"cmd": "settings"}, timeout=5)
        except ipc.DaemonNotRunning:
            return local_settings()
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": str(e)}
        if r.get("ok"):
            r["online"] = True
            return r
        data = local_settings()  # an older daemon, or it failed: edit the file directly
        if data.get("ok"):
            data["online"] = True
        return data

    def ui_font(tabular=False):
        f = QFont()
        f.setPixelSize(15)
        f.setWeight(QFont.Medium)
        if tabular:
            try:
                f.setFeature(QFont.Tag("tnum"), 1)
            except Exception:  # noqa: BLE001 - Qt < 6.7
                pass
        return f

    def divider():
        d = QFrame()
        d.setFixedSize(1, 24)
        d.setStyleSheet(f"background: {BORDER};")
        return d

    def repolish(w):
        w.style().unpolish(w)
        w.style().polish(w)
        w.update()

    class Option(QPushButton):
        def __init__(self, index, seconds, text):
            super().__init__(text)
            self.index, self.seconds = index, seconds
            self.setFocusPolicy(Qt.StrongFocus)
            self.setMinimumWidth(OPTION_MIN_WIDTH)
            self.setFixedHeight(BAR_HEIGHT)
            self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            self.setCursor(Qt.PointingHandCursor)
            self.setFont(ui_font(tabular=True))
            self.setProperty("long", False)

        def set_long(self, long):
            if self.property("long") != long:
                self.setProperty("long", long)
                repolish(self)

    class TextButton(QPushButton):
        def __init__(self, text, height=BAR_HEIGHT):
            super().__init__(text)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFixedHeight(height)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.setCursor(Qt.PointingHandCursor)
            self.setFont(ui_font())

    class IconButton(QPushButton):
        """gear / pause / play / stop, painted with QPainter so it never depends on a font."""

        TIPS = {"gear": "Settings (S)", "pause": "Pause recording (P)", "play": "Resume recording (P)",
                "stop": "Stop Momento"}

        def __init__(self, kind):
            super().__init__("")
            self.kind = kind
            self.setObjectName("icon")
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFixedSize(ICON_W, BAR_HEIGHT)
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip(self.TIPS[kind])
            self.setAccessibleName(self.TIPS[kind])

        def set_kind(self, kind):
            if kind != self.kind:
                self.kind = kind
                self.setToolTip(self.TIPS[kind])
                self.setAccessibleName(self.TIPS[kind])
                self.update()

        def paintEvent(self, ev):
            super().paintEvent(ev)  # stylesheet background: inverted on focus/hover
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            on = self.hasFocus() or self.underMouse()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#111111" if on else MUTED))
            c = QRectF(self.rect()).center()
            x, y = c.x(), c.y()
            if self.kind == "gear":
                p.drawPath(_gear_path(x, y))
            elif self.kind == "pause":
                p.drawRoundedRect(QRectF(x - 5, y - 6.5, 3.5, 13), 1, 1)
                p.drawRoundedRect(QRectF(x + 1.5, y - 6.5, 3.5, 13), 1, 1)
            elif self.kind == "play":
                from PySide6.QtCore import QPointF
                from PySide6.QtGui import QPolygonF
                p.drawPolygon(QPolygonF([QPointF(x - 4, y - 6.5), QPointF(x - 4, y + 6.5), QPointF(x + 6.5, y)]))
            elif self.kind == "stop":
                p.drawRoundedRect(QRectF(x - 5.5, y - 5.5, 11, 11), 1.5, 1.5)
            p.end()

    class SegButton(QPushButton):
        def __init__(self, text="", tip=None):
            super().__init__(text)
            self.setObjectName("seg")
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFixedHeight(ROW_H)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.setCursor(Qt.PointingHandCursor)
            self.setFont(ui_font())
            self.setProperty("sel", False)
            if tip:
                self.setToolTip(tip)

        def set_sel(self, on):
            if self.property("sel") != on:
                self.setProperty("sel", on)
                repolish(self)

    class SettingRow(QWidget):
        """A label and a segmented choice. Long lists collapse to ‹ current ›."""

        def __init__(self, bar, key, title, choices, value, avail, cycle=False):
            super().__init__()
            self.bar, self.key = bar, key
            self.values = [c[0] for c in choices]
            self.labels = [c[1] for c in choices]
            self.idx = self.values.index(value) if value in self.values else 0
            self.cycle = cycle
            self.setFixedHeight(ROW_H)
            lay = QHBoxLayout(self)
            lay.setContentsMargins(16, 0, 12, 0)
            lay.setSpacing(SEG_SPACING)
            t = QLabel(title)
            t.setObjectName("muted")
            t.setFixedWidth(LABEL_W)
            lay.addWidget(t)
            fm = QFontMetrics(ui_font())  # the buttons' font; the row is not styled yet
            pad = 2 * SEG_PAD + 2
            if cycle:
                self.prev, self.cur, self.next = SegButton("‹", "Previous"), SegButton(), SegButton("›", "Next")
                for b in (self.prev, self.next):
                    b.setFixedWidth(ARROW_W)
                    b.setFocusPolicy(Qt.ClickFocus)
                self.cur_room = avail - 2 * (ARROW_W + SEG_SPACING)
                widest = max(fm.horizontalAdvance(lbl) for lbl in self.labels) + pad + 4
                self.cur.setFixedWidth(max(80, min(widest, self.cur_room)))
                self.prev.clicked.connect(lambda: self.step(-1))
                self.next.clicked.connect(lambda: self.step(1))
                self.cur.clicked.connect(lambda: self.step(1))
                lay.addWidget(self.prev)
                lay.addWidget(self.cur)
                lay.addWidget(self.next)
                self.buttons = [self.cur]
            else:
                elide = [len(c) > 2 and c[2] for c in choices]
                natural = [fm.horizontalAdvance(lbl) + pad for lbl in self.labels]
                total = sum(natural) + SEG_SPACING * (len(natural) - 1)
                n_el = sum(elide)
                budget = None
                if total > avail and n_el:
                    fixed = sum(w for w, e in zip(natural, elide) if not e) + SEG_SPACING * (len(natural) - 1)
                    budget = max(48, (avail - fixed) // n_el - pad)
                self.buttons = []
                for i, lbl in enumerate(self.labels):
                    text = lbl
                    if budget is not None and elide[i]:
                        text = fm.elidedText(lbl, Qt.ElideRight, budget)
                    b = SegButton(text, lbl if text != lbl else None)
                    b.setFixedWidth(fm.horizontalAdvance(text) + pad)
                    b.clicked.connect(lambda _=False, i=i: self.select(i))
                    lay.addWidget(b)
                    self.buttons.append(b)
            lay.addStretch(1)
            self.refresh()

        @property
        def value(self):
            return self.values[self.idx]

        def refresh(self):
            if self.cycle:
                lbl = self.labels[self.idx]
                text = self.cur.fontMetrics().elidedText(lbl, Qt.ElideRight, self.cur.maximumWidth() - 2 * SEG_PAD - 2)
                self.cur.setText(text)
                self.cur.setToolTip(lbl if text != lbl else "")
                self.cur.set_sel(True)
            else:
                for i, b in enumerate(self.buttons):
                    b.set_sel(i == self.idx)

        def focus(self):
            (self.cur if self.cycle else self.buttons[self.idx]).setFocus(Qt.TabFocusReason)

        def select(self, i):
            changed = i != self.idx
            self.idx = i
            self.refresh()
            self.focus()
            if changed:
                self.bar.on_row_changed(self)

        def step(self, d):
            n = len(self.values)
            i = (self.idx + d) % n if self.cycle else max(0, min(n - 1, self.idx + d))
            self.select(i)

    class Bar(QWidget):
        def __init__(self):
            super().__init__()
            self.saving = False
            self.done = False
            self.online = None
            self.running = False      # daemon reachable
            self.paused = False
            self.buffered = 0.0
            self.status_inflight = False
            self.last_status = None
            self.mode = "clip"        # clip | settings | confirm
            self.control_busy = False  # pause / resume / start / stop in flight
            self.stopping = False
            self.loading_settings = False
            self.apply_state = None   # None | "busy" | "done"
            self.sdata = None
            self.rows = []
            self.layered = False
            self.bridge = Bridge()
            self.bridge.status.connect(self.on_status)
            self.bridge.saved.connect(self.on_saved)
            self.bridge.settings.connect(self.on_settings)
            self.bridge.configured.connect(self.on_configured)
            self.bridge.control.connect(self.on_control)
            self.bridge.started.connect(self.on_started)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAutoFillBackground(False)
            self.setWindowTitle("Replay")
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFont(ui_font())
            self.setStyleSheet(f"""
                QWidget {{ color: {TEXT}; background: transparent; }}
                QLabel#muted {{ color: {MUTED}; }}
                QLabel#dim {{ color: {DIM}; }}
                QPushButton {{ color: {TEXT}; border: none; border-radius: 6px; outline: none;
                               margin: 7px 0; padding: 0 10px; }}
                QPushButton#icon {{ padding: 0; }}
                QPushButton#quiet {{ color: {MUTED}; }}
                QPushButton[long="true"] {{ color: {DIM}; }}
                QPushButton#seg {{ color: {MUTED}; margin: 3px 0; padding: 0 {SEG_PAD}px; }}
                QPushButton#seg[sel="true"] {{ color: {TEXT}; background: {SEL_BG}; }}
                QPushButton:hover, QPushButton:focus,
                QPushButton#quiet:hover, QPushButton#quiet:focus,
                QPushButton#seg:hover, QPushButton#seg:focus,
                QPushButton#seg[sel="true"]:hover, QPushButton#seg[sel="true"]:focus
                    {{ background: {TEXT}; color: #111111; }}
            """)

            outer = QVBoxLayout(self)
            outer.setContentsMargins(1, 1, 1, 1)
            outer.setSpacing(0)

            # Above the bar (bottom-anchored, so the bar grows upward):
            # a one-line hint while paused, or the settings rows.
            self.hintbar = QLabel("Paused. Saving uses the footage so far"
                                  " · Resuming starts a fresh replay")
            self.hintbar.setObjectName("muted")
            self.hintbar.setContentsMargins(18, 0, 16, 0)
            self.hintbar.setFixedHeight(HINT_H)
            self.hintbar.hide()
            outer.addWidget(self.hintbar)
            self.panel = QWidget()
            self.panel_lay = QVBoxLayout(self.panel)
            self.panel_lay.setContentsMargins(0, PANEL_PAD_T, 0, PANEL_PAD_B)
            self.panel_lay.setSpacing(0)
            self.panel.hide()
            outer.addWidget(self.panel)
            self.sep = QFrame()
            self.sep.setFixedHeight(1)
            self.sep.setStyleSheet(f"background: {BORDER}; margin: 0 12px;")
            self.sep.hide()
            outer.addWidget(self.sep)

            self.stack = QStackedWidget()
            self.stack.setFixedHeight(BAR_HEIGHT)
            outer.addWidget(self.stack)

            # page 0: picker
            picker = QWidget()
            row = QHBoxLayout(picker)
            row.setContentsMargins(16, 0, 0, 0)
            row.setSpacing(0)
            self.dot = QLabel()
            self.dot.setFixedSize(8, 8)
            row.addWidget(self.dot)
            row.addSpacing(10)
            self.name = QLabel("Replay")
            self.name.setObjectName("muted")
            self.name.setMinimumWidth(self.name.fontMetrics().horizontalAdvance("Paused") + 2)
            row.addWidget(self.name)
            row.addSpacing(10)
            self.time = QLabel("0:00")
            self.time.setFont(ui_font(tabular=True))
            self.time.setMinimumWidth(self.time.fontMetrics().horizontalAdvance("00:00") + 2)
            row.addWidget(self.time)
            row.addSpacing(12)
            row.addWidget(divider())
            row.addSpacing(6)
            self.options = []
            for i, (secs, text) in enumerate(PRESETS):
                o = Option(i, secs, text)
                o.clicked.connect(lambda _=False, o=o: self.choose(o))
                row.addWidget(o)
                self.options.append(o)
            row.addSpacing(6)
            row.addWidget(divider())
            row.addSpacing(4)
            self.controls = [self._controls(row)]
            row.addSpacing(10)
            self.stack.addWidget(picker)

            # page 1: a single line (saving / saved / error / off) + controls
            line = QWidget()
            lrow = QHBoxLayout(line)
            lrow.setContentsMargins(18, 0, 0, 0)
            lrow.setSpacing(0)
            self.line = QLabel("")
            self.line.setTextFormat(Qt.RichText)
            self.line.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lrow.addWidget(self.line, 1)
            self.start_btn = TextButton("Start")
            self.start_btn.setToolTip("Start recording")
            self.start_btn.clicked.connect(self.start_recorder)
            self.start_btn.hide()
            lrow.addWidget(self.start_btn)
            lrow.addSpacing(4)
            self.controls.append(self._controls(lrow))
            lrow.addSpacing(10)
            self.stack.addWidget(line)

            # page 2: settings footer (estimate / status + Apply / Back)
            foot = QWidget()
            frow = QHBoxLayout(foot)
            frow.setContentsMargins(18, 0, 0, 0)
            frow.setSpacing(0)
            self.foot = QLabel("")
            self.foot.setTextFormat(Qt.RichText)
            self.foot.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            frow.addWidget(self.foot, 1)
            self.apply_btn = TextButton("Apply")
            self.apply_btn.clicked.connect(self.apply_settings)
            self.back_btn = TextButton("Back")
            self.back_btn.setObjectName("quiet")
            self.back_btn.clicked.connect(self.close_settings)
            frow.addWidget(self.apply_btn)
            frow.addSpacing(2)
            frow.addWidget(self.back_btn)
            frow.addSpacing(10)
            self.stack.addWidget(foot)

            # page 3: stop confirmation
            conf = QWidget()
            crow = QHBoxLayout(conf)
            crow.setContentsMargins(18, 0, 0, 0)
            crow.setSpacing(0)
            self.confirm = QLabel(f"Stop Momento?&nbsp;&nbsp;<span style='color:{MUTED}'>"
                                  "Recording ends and the replay buffer is cleared.</span>")
            self.confirm.setTextFormat(Qt.RichText)
            self.confirm.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            crow.addWidget(self.confirm, 1)
            self.stop_yes = TextButton("Stop")
            self.stop_yes.clicked.connect(self.confirm_stop)
            self.stop_no = TextButton("Cancel")
            self.stop_no.setObjectName("quiet")
            self.stop_no.clicked.connect(self.cancel_confirm)
            crow.addWidget(self.stop_yes)
            crow.addSpacing(2)
            crow.addWidget(self.stop_no)
            crow.addSpacing(10)
            self.stack.addWidget(conf)

            # Width is fixed to the picker so the bar never jumps between states;
            # only the height changes (upward) for the hint line and settings.
            self.bar_w = picker.sizeHint().width() + 2
            self.setFixedSize(self.bar_w, BAR_HEIGHT + 2)
            self.set_controls(running=False, paused=False, start=False)

            self.idle = QTimer(self)
            self.idle.setSingleShot(True)
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.idle.timeout.connect(self.on_idle)
            self.poll = QTimer(self)
            self.poll.setInterval(1000)
            self.poll.timeout.connect(self.refresh_async)

        def _controls(self, lay):
            gear, pause, stop = IconButton("gear"), IconButton("pause"), IconButton("stop")
            gear.clicked.connect(self.open_settings)
            pause.clicked.connect(self.toggle_pause)
            stop.clicked.connect(self.ask_stop)
            for b in (gear, pause, stop):
                lay.addWidget(b)
            return {"gear": gear, "pause": pause, "stop": stop}

        @property
        def gear(self):
            return self.controls[0]["gear"]

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(BORDER), 1))
            p.setBrush(QColor(*BG))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
            p.end()

        # ---------------- layout
        def relayout(self):
            top = 0
            show_hint = self.mode == "clip" and self.paused and self.running and not self.done
            self.hintbar.setHidden(not show_hint)
            if show_hint:
                top += HINT_H
            if self.mode == "settings":
                n = sum(1 for r in self.rows if not r.isHidden())
                ph = PANEL_PAD_T + HEADER_H + n * ROW_H + PANEL_PAD_B
                self.panel.setFixedHeight(ph)
                self.panel.show()
                top += ph
            else:
                self.panel.hide()
            self.sep.setHidden(top == 0)
            h = BAR_HEIGHT + 2 + top + (1 if top else 0)
            if h == self.height():
                return
            bottom = self.y() + self.height()
            self.setFixedSize(self.bar_w, h)
            if not self.layered and self.isVisible():
                # A plain window keeps its bottom edge; a layer surface is
                # bottom-anchored by the compositor and grows upward by itself.
                self.move(self.x(), bottom - h)

        def show_line(self, markup, focus=None):
            self.line.setText(markup)
            if self.stack.currentIndex() != 1:
                self.stack.setCurrentIndex(1)
                (focus or self).setFocus(Qt.OtherFocusReason)
            elif focus is not None:
                w = QApplication.focusWidget()
                if w is None or w is self or w.isHidden():
                    focus.setFocus(Qt.OtherFocusReason)

        def set_controls(self, running, paused, start):
            for c in self.controls:
                c["pause"].setHidden(not running)
                c["stop"].setHidden(not running)
                c["pause"].set_kind("play" if paused else "pause")
            self.start_btn.setHidden(not start)
            self.dot.setStyleSheet(f"background: {MUTED if paused else RED}; border-radius: 4px;")
            self.name.setText("Paused" if paused else "Replay")

        # ---------------- status
        def refresh_async(self):
            if self.status_inflight or self.saving or self.done:
                return
            self.status_inflight = True

            def work():
                self.bridge.status.emit(fetch_status())
            threading.Thread(target=work, daemon=True).start()

        def on_status(self, st):
            self.status_inflight = False
            self.apply_status(st)

        def apply_status(self, st):
            self.last_status = st
            if self.saving or self.done or self.mode != "clip" or self.control_busy:
                return
            if self.stopping:
                if st.get("ok"):
                    return  # still shutting down
                self.stopping = False
            if not st.get("ok"):
                self.online = False
                self.running = False
                self.paused = False
                self.buffered = 0.0
                off = st.get("not_running", True)
                self.set_controls(running=False, paused=False, start=off)
                if off:
                    self.show_line(f"<span style='color:{MUTED}'>Replay is off</span>", focus=self.start_btn)
                else:
                    self.show_line(f"<span style='color:{RED}'>"
                                   f"{_esc(st.get('error') or 'Cannot reach the recorder')}</span>")
                self.relayout()
                return
            self.running = True
            self.paused = st.get("state") == "paused"
            self.set_controls(running=True, paused=self.paused, start=False)
            self.buffered = float(st.get("buffered") or 0.0)
            self.time.setText(_mmss(self.buffered))
            self.dot.setVisible(bool(st.get("recording")) or self.paused)
            self.relayout()
            if self.buffered <= 0:
                self.online = False
                if self.paused:
                    msg = "Paused — nothing was buffered"
                elif st.get("recording"):
                    msg = "Recording — nothing buffered yet"
                else:
                    msg = f"Replay is {_esc(st.get('state') or 'idle')} — nothing buffered"
                self.show_line(f"<span style='color:{MUTED}'>{msg}</span>")
                return
            first = self.online is not True or self.stack.currentIndex() != 0
            self.online = True
            self.stack.setCurrentIndex(0)
            for o in self.options:
                o.set_long(o.seconds > self.buffered + 0.5)
            if first:
                self.focus_default()

        def focus_default(self):
            if self.stack.currentIndex() != 0:
                if not self.start_btn.isHidden() and self.stack.currentIndex() == 1:
                    self.start_btn.setFocus(Qt.OtherFocusReason)
                else:
                    self.setFocus(Qt.OtherFocusReason)
                return
            want = _last_choice()
            target = next((o for o in self.options if o.seconds == want), self.options[0])
            target.setFocus(Qt.OtherFocusReason)

        # ---------------- save
        def choose(self, opt):
            if self.saving or self.done or not self.online or self.mode != "clip" or self.control_busy:
                return
            self.saving = True
            self.idle.stop()
            _store_choice(opt.seconds)
            shown = min(opt.seconds, self.buffered) if self.buffered else opt.seconds
            self.show_line(f"Saving last {dur_label(shown)}…")
            self.hintbar.hide()
            self.relayout()
            secs = opt.seconds

            def work():
                try:
                    r = ipc.request({"cmd": "save", "seconds": secs}, timeout=120)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.saved.emit(r)
            threading.Thread(target=work, daemon=True).start()

        def on_saved(self, r):
            self.saving = False
            self.done = True
            self.relayout()
            fm = self.line.fontMetrics()
            room = max(120, self.line.width())
            if r.get("ok"):
                name = Path(str(r.get("path", ""))).name or "clip"
                name = fm.elidedText(name, Qt.ElideMiddle, room - fm.horizontalAdvance("Saved    "))
                self.show_line(f"Saved&nbsp;&nbsp;<span style='color:{MUTED}'>{_esc(name)}</span>")
            else:
                err = fm.elidedText(str(r.get("error") or "Save failed"), Qt.ElideRight, room)
                self.show_line(f"<span style='color:{RED}'>{_esc(err)}</span>")
            QTimer.singleShot(RESULT_CLOSE_MS, QApplication.instance().quit)

        # ---------------- pause / resume / stop / start
        def toggle_pause(self):
            if not self.running or self.control_busy or self.saving or self.done or self.mode != "clip":
                return
            cmd = "resume" if self.paused else "pause"
            self.control_busy = True

            def work():
                try:
                    r = ipc.request({"cmd": cmd}, timeout=30)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit(cmd, r)
            threading.Thread(target=work, daemon=True).start()

        def ask_stop(self):
            if not self.running or self.control_busy or self.saving or self.done or self.mode != "clip":
                return
            self.mode = "confirm"
            self.stack.setCurrentIndex(3)
            self.relayout()
            self.stop_no.setFocus(Qt.OtherFocusReason)  # the safe choice is the default

        def cancel_confirm(self):
            if self.mode != "confirm" or self.control_busy:
                return
            self.mode = "clip"
            self.back_to_clip("stop")

        def confirm_stop(self):
            if self.mode != "confirm" or self.control_busy:
                return
            self.control_busy = True
            self.mode = "clip"
            self.hintbar.hide()
            self.show_line("Stopping Momento…")
            self.relayout()

            def work():
                try:
                    r = ipc.request({"cmd": "quit"}, timeout=10)
                except ipc.DaemonNotRunning:
                    r = {"ok": True}
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit("quit", r)
            threading.Thread(target=work, daemon=True).start()

        def on_control(self, cmd, r):
            self.control_busy = False
            if not r.get("ok"):
                self.show_line(f"<span style='color:{RED}'>{_esc(r.get('error') or cmd + ' failed')}</span>")
                return
            if cmd == "quit":
                self.stopping = True
                self.running = False
                self.set_controls(running=False, paused=False, start=False)
            elif cmd == "pause" and self.last_status and self.last_status.get("ok"):
                # show it right away; the next poll confirms
                self.apply_status({**self.last_status, "state": "paused", "recording": False})
            elif cmd == "resume":
                self.apply_status({**(self.last_status or {}), "ok": True, "state": "starting",
                                   "recording": False, "buffered": 0})
            self.status_inflight = False
            self.refresh_async()

        def start_recorder(self):
            if self.running or self.control_busy or self.saving or self.done:
                return
            self.control_busy = True
            self.start_btn.hide()
            self.show_line(f"<span style='color:{MUTED}'>Starting Momento…</span>")

            def work():
                try:
                    start_daemon()
                except Exception as e:  # noqa: BLE001
                    self.bridge.started.emit({"ok": False, "error": f"cannot start Momento: {e}"})
                    return
                deadline = time.monotonic() + START_TIMEOUT_S
                st = None
                while time.monotonic() < deadline:
                    st = fetch_status(timeout=1.0)
                    if st.get("ok") and (st.get("recording") or float(st.get("buffered") or 0) > 0
                                         or st.get("state") == "error"):
                        break
                    time.sleep(START_POLL_S)
                if not st or not st.get("ok"):
                    st = {"ok": False, "not_running": False,
                          "error": "Momento did not start — see: journalctl --user -u momento"}
                self.bridge.started.emit(st)
            threading.Thread(target=work, daemon=True).start()

        def on_started(self, st):
            self.control_busy = False
            self.online = None
            self.apply_status(st)

        def back_to_clip(self, focus_key=None):
            self.mode = "clip"
            self.online = None
            st = self.last_status or {"ok": False, "not_running": True}
            self.apply_status(st)
            self.relayout()
            page = self.stack.currentIndex()
            if focus_key and page in (0, 1) and not self.controls[page][focus_key].isHidden():
                self.controls[page][focus_key].setFocus(Qt.OtherFocusReason)

        # ---------------- settings
        def open_settings(self):
            if self.mode != "clip" or self.saving or self.done or self.loading_settings or self.control_busy:
                return
            self.loading_settings = True

            def work():
                self.bridge.settings.emit(fetch_settings())
            threading.Thread(target=work, daemon=True).start()

        def on_settings(self, data):
            self.loading_settings = False
            if self.mode != "clip" or self.saving or self.done:
                return
            if not data.get("ok"):
                self.show_line(f"<span style='color:{RED}'>{_esc(data.get('error') or 'Cannot read settings')}</span>")
                return
            self.sdata = data
            self.build_rows()
            self.mode = "settings"
            self.apply_state = None
            self.stack.setCurrentIndex(2)
            self.update_foot()
            self.relayout()
            self.rows[0].focus()
            self.idle.setInterval(SETTINGS_IDLE_MS)
            self.idle.start()

        def build_rows(self):
            old = self.panel_lay.takeAt(0)
            while old is not None:
                if old.widget() is not None:
                    old.widget().deleteLater()
                old = self.panel_lay.takeAt(0)
            data = self.sdata
            vals = data["values"]
            dev = data.get("devices") or {}
            outs, ins = dev.get("outputs") or [], dev.get("inputs") or []
            avail = self.bar_w - 2 - 16 - LABEL_W - SEG_SPACING - 12

            header = QWidget()
            header.setFixedHeight(HEADER_H)
            hl = QHBoxLayout(header)
            hl.setContentsMargins(18, 0, 18, 0)
            title = QLabel("Settings")
            hl.addWidget(title)
            hl.addStretch(1)
            self.note = QLabel("Applying restarts the replay buffer" if data.get("online")
                               else "Momento is off — changes apply when it starts")
            self.note.setObjectName("dim")
            hl.addWidget(self.note)
            self.panel_lay.addWidget(header)

            res = [(r, RES_LABELS.get(r, r)) for r in data["choices"]["resolution"]]
            qual = [(q, q.capitalize()) for q in data["choices"]["quality"]]
            sound = [("default", "Default output")] + [(d["name"], d["label"], True) for d in outs]
            if vals["audio_source"] not in [c[0] for c in sound] + ["off"]:
                sound.append((vals["audio_source"], vals["audio_source"], True))  # unplugged device
            sound.append(("off", "Off"))
            micdev = [("default", "Default mic")] + [(d["name"], d["label"], True) for d in ins]
            if vals["mic_device"] not in [c[0] for c in micdev]:
                micdev.append((vals["mic_device"], vals["mic_device"], True))
            self.rows = [
                SettingRow(self, "resolution", "Resolution", res, vals["resolution"], avail),
                SettingRow(self, "quality", "Quality", qual, vals["quality"], avail),
                SettingRow(self, "audio_source", "Sound", sound, vals["audio_source"], avail,
                           cycle=len(sound) - 2 > CYCLE_OVER),
                SettingRow(self, "mic", "Mic", [("off", "Off"), ("on", "On")], vals["mic"], avail),
                SettingRow(self, "mic_device", "Mic device", micdev, vals["mic_device"], avail,
                           cycle=len(micdev) - 1 > CYCLE_OVER),
            ]
            for r in self.rows:
                self.panel_lay.addWidget(r)
            self.row("mic_device").setHidden(vals["mic"] != "on")

        def row(self, key):
            return next(r for r in self.rows if r.key == key)

        def visible_rows(self):
            return [r for r in self.rows if not r.isHidden()]

        def pending(self):
            return {r.key: r.value for r in self.rows}

        def changes(self):
            vals = self.sdata["values"]
            return {k: v for k, v in self.pending().items() if vals.get(k) != v}

        def estimate(self):
            v = self.pending()
            cap = {"resolution": v["resolution"], "quality": v["quality"],
                   "bitrate_kbps": self.sdata["values"].get("bitrate", 0)}
            secs = int(self.sdata.get("max_seconds") or 3600)
            gb = quality.buffer_gb(quality.bitrate_kbps(cap), secs)
            return f"{self.sdata.get('fps', quality.FPS)} fps · ~{gb:.1f} GB for {secs // 60} min"

        def update_foot(self):
            if self.apply_state is None:
                self.foot.setText(f"<span style='color:{MUTED}'>{_esc(self.estimate())}</span>")

        def on_row_changed(self, row):
            if row.key == "mic":
                self.row("mic_device").setHidden(row.value != "on")
                self.relayout()
            if self.apply_state == "error":
                self.apply_state = None
            self.update_foot()

        def close_settings(self):
            if self.mode != "settings" or self.apply_state == "busy":
                return
            self.apply_state = None
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.idle.start()
            self.back_to_clip("gear")
            self.refresh_async()

        def apply_settings(self):
            if self.mode != "settings" or self.apply_state in ("busy", "done"):
                return
            changes = self.changes()
            if not changes:
                self.close_settings()
                return
            self.apply_state = "busy"
            self.idle.stop()
            self.foot.setText("Applying…")
            online, path = bool(self.sdata.get("online")), self.sdata.get("config")

            def local():
                try:
                    settings.apply(changes, path)
                    return {"ok": True, "online": False}
                except (OSError, ValueError) as e:
                    return {"ok": False, "error": str(e)}

            def work():
                if online:
                    try:
                        r = ipc.request({"cmd": "configure", "changes": changes}, timeout=60)
                        r.setdefault("online", True)
                    except ipc.DaemonNotRunning:
                        r = local()
                    except Exception as e:  # noqa: BLE001
                        r = {"ok": False, "error": str(e) or e.__class__.__name__}
                else:
                    r = local()
                self.bridge.configured.emit(r)
            threading.Thread(target=work, daemon=True).start()

        def on_configured(self, r):
            if self.mode != "settings":
                return
            if not r.get("ok"):
                self.apply_state = "error"
                fm = self.foot.fontMetrics()
                err = fm.elidedText(str(r.get("error") or "Could not save"), Qt.ElideRight,
                                    max(120, self.foot.width()))
                self.foot.setText(f"<span style='color:{RED}'>{_esc(err)}</span>")
                self.idle.start()
                return
            self.apply_state = "done"
            if not r.get("online"):
                tail = "takes effect when Momento starts"
            elif r.get("paused"):
                tail = "applies when you resume"
            else:
                tail = "recording restarted"
            self.foot.setText(f"Saved&nbsp;<span style='color:{MUTED}'>— {tail}</span>")
            if r.get("online") and not r.get("paused"):
                self.buffered = 0.0  # the buffer started over
                self.last_status = {**(self.last_status or {}), "ok": True, "state": "starting",
                                    "recording": False, "buffered": 0}
            QTimer.singleShot(RESULT_CLOSE_MS, self.after_apply)

        def after_apply(self):
            if self.mode == "settings" and self.apply_state == "done":
                self.apply_state = None
                self.close_settings()

        def settings_row_index(self):
            w = QApplication.focusWidget()
            rows = self.visible_rows()
            for i, r in enumerate(rows):
                if w is not None and r.isAncestorOf(w):
                    return i
            if w in (self.apply_btn, self.back_btn):
                return len(rows)
            return 0

        def settings_key(self, k):
            if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
                self.close_settings()
                return True
            if self.apply_state in ("busy", "done"):
                return True
            rows = self.visible_rows()
            i = self.settings_row_index()
            if k in (Qt.Key_Up, Qt.Key_Backtab, Qt.Key_Down, Qt.Key_Tab):
                i = max(0, min(len(rows), i + (-1 if k in (Qt.Key_Up, Qt.Key_Backtab) else 1)))
                if i < len(rows):
                    rows[i].focus()
                else:
                    self.apply_btn.setFocus(Qt.TabFocusReason)
            elif k in (Qt.Key_Left, Qt.Key_Right):
                d = -1 if k == Qt.Key_Left else 1
                if i < len(rows):
                    rows[i].step(d)
                else:
                    (self.apply_btn if d < 0 else self.back_btn).setFocus(Qt.TabFocusReason)
            elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select):
                if self.back_btn.hasFocus():
                    self.close_settings()
                else:
                    self.apply_settings()
            return True

        def confirm_key(self, k):
            if self.control_busy:
                return True
            if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
                self.cancel_confirm()
            elif k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Tab, Qt.Key_Backtab, Qt.Key_Up, Qt.Key_Down):
                (self.stop_no if self.stop_yes.hasFocus() else self.stop_yes).setFocus(Qt.TabFocusReason)
            elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select):
                if self.stop_yes.hasFocus():
                    self.confirm_stop()
                else:
                    self.cancel_confirm()
            return True

        # ---------------- input
        def on_idle(self):
            if self.apply_state == "busy" or self.control_busy:
                self.idle.start()
                return
            self.request_close()

        def request_close(self):
            if self.saving:
                return  # let the save finish so the result is visible
            QApplication.instance().quit()

        def focusables(self):
            page = self.stack.currentIndex()
            if page not in (0, 1):
                return []
            c = self.controls[page]
            items = (self.options if page == 0 else [self.start_btn]) + [c["gear"], c["pause"], c["stop"]]
            return [w for w in items if not w.isHidden()]

        def move_focus(self, step):
            items = self.focusables()
            if not items:
                return
            cur = next((i for i, w in enumerate(items) if w.hasFocus()), None)
            if cur is None:
                if self.stack.currentIndex() == 0:
                    self.focus_default()
                else:
                    items[0 if step > 0 else -1].setFocus(Qt.TabFocusReason)
                return
            items[(cur + step) % len(items)].setFocus(Qt.TabFocusReason)

        def handle_key(self, ev) -> bool:
            k = ev.key()
            if self.mode == "settings":
                return self.settings_key(k)
            if self.mode == "confirm":
                return self.confirm_key(k)
            if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
                self.request_close()
                return True
            if self.saving or self.done or self.control_busy:
                return True
            if k in (Qt.Key_Left, Qt.Key_Backtab, Qt.Key_Up):
                self.move_focus(-1)
            elif k in (Qt.Key_Right, Qt.Key_Tab, Qt.Key_Down):
                self.move_focus(1)
            elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select):
                w = QApplication.focusWidget()
                if w in self.options:
                    self.choose(w)
                elif w in self.focusables():
                    w.click()
            elif Qt.Key_1 <= k <= Qt.Key_8:
                idx = k - Qt.Key_1
                if idx < len(self.options) and self.online:
                    self.options[idx].setFocus(Qt.ShortcutFocusReason)
                    self.choose(self.options[idx])
            elif k == Qt.Key_S:
                self.open_settings()
            elif k == Qt.Key_P:
                self.toggle_pause()
            else:
                return False
            return True

        def eventFilter(self, obj, ev):
            t = ev.type()
            if t in (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.TouchBegin):
                if not self.saving and not self.done:
                    self.idle.start()  # restart the idle auto-close
            if t == QEvent.KeyPress and self.isVisible():
                return self.handle_key(ev)
            return False

    return Bar, fetch_status


def main(argv=None) -> int:
    argv = list(argv or [])
    logging.basicConfig(level=logging.INFO, format="momento overlay: %(message)s")

    if _toggle_existing():
        return 0

    use_layer_shell = _layer_shell_available()
    if use_layer_shell:
        os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["momento-overlay"] + argv)
    app.setApplicationName("Momento")
    app.setDesktopFileName("io.github.mehulchachada.Momento")

    _write_pidfile()
    try:
        signal.signal(signal.SIGTERM, lambda *_: app.quit())
        signal.signal(signal.SIGINT, lambda *_: app.quit())
    except ValueError:
        pass  # not the main thread
    # Let the Python interpreter run signal handlers while Qt's loop spins.
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(200)

    Bar, fetch_status = _build(argv)
    bar = Bar()
    bar.layered = use_layer_shell
    app.installEventFilter(bar)
    bar.apply_status(fetch_status(timeout=1.0))

    layered = False
    if use_layer_shell:
        try:
            bar.winId()  # create the QWindow before configuring the surface
            layered = _apply_layer_shell(bar)
        except Exception as e:  # noqa: BLE001
            log.warning("layer-shell setup failed (%s); using a normal window", e)
    if not layered:
        bar.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        bar.setAttribute(Qt.WA_TranslucentBackground)
        screen = app.primaryScreen()
        if screen is not None:
            g = screen.availableGeometry()  # stays clear of a bottom taskbar
            bar.move(g.x() + (g.width() - bar.width()) // 2, g.y() + g.height() - bar.height() - BOTTOM_MARGIN)
    bar.show()
    bar.activateWindow()
    bar.raise_()
    bar.focus_default()
    bar.idle.start()
    bar.poll.start()

    auto = os.environ.get("MOMENTO_OVERLAY_AUTOCLOSE")
    if auto:
        try:
            QTimer.singleShot(int(float(auto) * 1000), app.quit)
        except ValueError:
            pass

    bar.layered = layered
    main.window = bar  # for tests / debugging
    main.layered = layered
    try:
        return app.exec()
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
