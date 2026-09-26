"""The replay picker: a slim bar at the bottom of the screen.

Two ways to run it:

* resident (``momento overlay --resident``, started and supervised by the
  daemon when ``[ui] keep_bar_loaded`` is on): the bar is built once and kept
  hidden; a small control socket (``$XDG_RUNTIME_DIR/overlay.sock``, the
  daemon's JSON-lines framing) takes ``toggle`` / ``show`` / ``hide`` /
  ``quit``, so the hotkey shows it within a frame. Every show starts from the
  same state a fresh bar would; closing hides it instead of quitting.
* one-shot (``momento overlay`` with no resident bar listening): a new
  process per open. Running it while another one-shot bar is open closes the
  open one instead, so the same key toggles it.

Besides the clip lengths the bar has four painted glyph buttons, in this
order: pause/resume (key P), stop (asks inline first), screenshot (camera,
only while recording) and settings (gear, key S). A screenshot hides the bar
first and then asks the daemon for the next recorded frame, so the bar is not
in the picture; the daemon's desktop notification confirms it. Settings
open in the same bar, which grows upward into a few segmented rows; applying
goes through the daemon's ``configure`` IPC, or straight to the config file
when the daemon is off.

Window mode (settings: Record -> Game window): when the picked window closes
the daemon reports "no_window"; the bar then shows "No window" and its play
button asks the daemon to open the window picker (``pick_window``). The bar
hides itself right after, so the desktop's picker dialog is usable.

On KDE/wlroots Wayland the bar is a wlr-layer-shell surface on the Overlay
layer (drawn above fullscreen games), anchored to the bottom edge and sized to
the bar itself so clicks elsewhere still reach the game. LayerShellQt has no
Python bindings, so it is driven through ctypes. Anywhere that fails, the bar
is a frameless always-on-top tool window at the bottom centre of the screen.
"""

from __future__ import annotations

import ctypes
import html
import json
import logging
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import OVERLAY_SOCKET, RUNTIME_DIR
from .durations import PRESETS, label as dur_label

log = logging.getLogger(__name__)

PIDFILE = RUNTIME_DIR / "overlay.pid"
CONTROL_SOCKET = OVERLAY_SOCKET   # the resident bar's control socket
LAST_FILE = RUNTIME_DIR / "overlay.last"
LAYER_SHELL_PLUGIN = "wayland-shell-integration/liblayer-shell.so"
LAYER_SHELL_LIB = "libLayerShellQtInterface.so.6"

BOTTOM_MARGIN = 24
BAR_HEIGHT = 52
PILL_H = 32              # every button is a pill (or a circle) this tall, centred in its row
PILL_INSET = 4           # each side: the gap between pills, with room for the focus ring
PILL_PAD = 14            # text padding inside a clip-length pill
OPTION_MIN_WIDTH = 60
IDLE_CLOSE_MS = 10_000   # no interaction: hide after this long
# After an interaction the result is shown briefly, then the bar hides:
RESULT_CLOSE_MS = 1_200  # "Saved <file>" or a save error
STOP_CLOSE_MS = 800      # the Off state after a confirmed Stop
APPLY_CLOSE_MS = 1_200   # "Saved — recording restarted" after Apply in settings
# Screenshot: the bar hides, then waits this long before asking for the frame, so
# the compositor has redrawn the screen without it (a few frames at 30-60 fps).
SHOT_DELAY_MS = 150
SHOT_TIMEOUT_S = 30
TICK_MS = 250            # the recording timer ticks locally between the 1 s status polls
DEFAULT_SECONDS = 60
ICON_W = PILL_H + 2 * PILL_INSET   # pause / stop / screenshot / gear: circles
HINT_H = 30              # the "Paused" line above the bar
HEADER_H = 34            # settings: title line
ROW_H = 40               # settings: one row
PANEL_PAD_T = 6
PANEL_PAD_B = 6
LABEL_W = 96             # settings: row label column
SEG_PAD = 12
SEG_SPACING = 0          # pills carry their own gap (PILL_INSET)
ARROW_W = PILL_H + 2 * PILL_INSET
CYCLE_OVER = 4           # more devices than this -> ‹ current › instead of a row of names
SETTINGS_IDLE_MS = 30_000
START_TIMEOUT_S = 10
START_POLL_S = 0.5
LOGO_SIZE = 18
ANIM_MS = 140            # pill fill / text colour transition

# Palette. The record dot is the only accent colour (the storage hint aside).
BG = (17, 17, 17, 240)   # #111111 at ~94 %
BORDER = "#2A2A2A"
TEXT = "#EDEDED"
MUTED = "#8A8A8A"
DIM = "#555555"
RED = "#FF4D2E"
PILL_REST = "#1C1C1C"    # a resting pill: just enough to see the shape
PILL_ON = "#F2F2F2"      # hover and keyboard focus
PILL_SEL = "#CFCFCF"     # the chosen value in a settings row, when not focused
ON_TEXT = "#111111"      # text on a white pill
RING = "#EDEDED"         # keyboard focus ring around a white pill
GREEN = "#4CC38A"
YELLOW = "#F5C542"

# Builds the bar's controller hub (momento.gamepad.Gamepads); tests swap in fakes.
PAD_FACTORY = None

PAUSED_HINT = "Paused · saving uses the footage so far"
NO_WINDOW_HINT = "Game closed · press play to pick a window"


def _gb(n) -> str:
    return f"{float(n or 0) / 1e9:.1f}"


def _storage_short(st) -> dict | None:
    """The status's storage block when free space is short, else None."""
    if not st or not st.get("ok"):
        return None
    sto = st.get("storage") if isinstance(st.get("storage"), dict) else None
    if st.get("state") == "no_storage" or (sto is not None and sto.get("ok") is False):
        return sto or {}
    return None


def _free_label(n) -> str:
    """Free space for the bar: '742 GB', '9.4 GB', '1.2 TB'."""
    n = float(n or 0)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= size:
            v = n / size
            return f"{v:.0f} {unit}" if v >= 100 else f"{v:.1f} {unit}"
    return "0 MB"


def _storage_level(sto) -> str | None:
    """'ok' (room for two full buffers), 'tight' (fits, but not twice) or 'short'
    (does not fit); None when the daemon sent no storage info."""
    if not isinstance(sto, dict) or sto.get("free") is None:
        return None
    room = float(sto.get("free") or 0) + float(sto.get("reclaimable") or 0)
    need = float(sto.get("required") or 0)
    if sto.get("ok") is False or (need and room < need):
        return "short"
    return "ok" if room >= 2 * need else "tight"


STORAGE_COLORS = {"ok": GREEN, "tight": YELLOW, "short": RED}

STORAGE_TAIL = "Free up space or pick a lower quality in settings"


def _storage_warning(sto, message=None) -> str:
    """One line for the strip above the bar; the daemon's own wording wins when it sent one."""
    if message:
        return f"{str(message).rstrip('. ')}. {STORAGE_TAIL}"
    free = float(sto.get("free") or 0) + float(sto.get("reclaimable") or 0)
    need = sto.get("required")
    if need:
        return f"Not enough free space: needs {_gb(need)} GB, {_gb(free)} GB free. {STORAGE_TAIL}"
    return f"Not enough free space. {STORAGE_TAIL}"


def _status_warning(st) -> str | None:
    sto = _storage_short(st)
    if sto is None:
        return None
    msg = st.get("error") if st.get("state") == "no_storage" else None
    return _storage_warning(sto, msg)


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


def send_resident(cmd: str, timeout: float = 2.0, path=None) -> dict | None:
    """Send ``cmd`` to the resident bar; None when no resident bar answers."""
    from . import ipc

    try:
        return ipc.request({"cmd": cmd}, timeout=timeout, path=path or CONTROL_SOCKET)
    except ipc.DaemonNotRunning:
        return None
    except (ipc.IPCError, OSError, ValueError) as e:
        log.warning("the resident clip bar did not answer: %s", e)
        return None


def toggle() -> bool:
    """Close an open one-shot bar, or toggle the resident one.

    False when neither exists: the caller then starts a one-shot bar.
    """
    if _toggle_existing():
        return True
    r = send_resident("toggle")
    return bool(r and r.get("ok"))


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


KEYBOARD_SYM = "_ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE"


def _layer_window(widget):
    """(library, LayerShellQt::Window*) for ``widget``'s QWindow, or None."""
    import shiboken6

    lib = ctypes.CDLL(LAYER_SHELL_LIB)
    handle = widget.windowHandle()
    if handle is None:
        return None
    # QWindow inherits QObject and QSurface -> one pointer per base; QObject first.
    qwindow_ptr = shiboken6.getCppPointer(handle)[0]
    get = lib._ZN12LayerShellQt6Window3getEP7QWindow
    get.restype = ctypes.c_void_p
    get.argtypes = [ctypes.c_void_p]
    lsw = get(ctypes.c_void_p(qwindow_ptr))
    return (lib, lsw) if lsw else None


def _set_keyboard_interactivity(widget, exclusive: bool) -> None:
    """Exclusive while the resident bar is shown, None while it is hidden.

    Hiding the QWindow already destroys the layer surface (so a hidden bar
    cannot hold the keyboard); this keeps the stored setting in step anyway,
    for compositors / LayerShellQt versions that only unmap it.
    """
    try:
        found = _layer_window(widget)
        if found is None:
            return
        lib, lsw = found
        fn = getattr(lib, KEYBOARD_SYM)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
        fn(ctypes.c_void_p(lsw), 1 if exclusive else 0)
    except Exception as e:  # noqa: BLE001
        log.debug("keyboard interactivity not changed: %s", e)


def _apply_layer_shell(widget) -> bool:
    """Make ``widget``'s window a bottom-anchored Overlay-layer surface.

    Must run after the QWindow exists (winId()) and before the first show().
    The surface size comes from the widget size; with only the bottom edge
    anchored the compositor centres it horizontally.
    """
    import shiboken6

    found = _layer_window(widget)
    if found is None:
        return False
    lib, lsw = found
    this = ctypes.c_void_p(lsw)

    def call(sym, value, argtype=ctypes.c_int):
        fn = getattr(lib, sym)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p, argtype]
        fn(this, value)

    call("_ZN12LayerShellQt6Window8setLayerENS0_5LayerE", 3)                        # Overlay
    call("_ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE", 2)           # Bottom only
    call(KEYBOARD_SYM, 1)                                                           # Exclusive
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


def _camera_path(cx: float, cy: float):
    """A filled camera (body, viewfinder hump, lens ring and lens) as one QPainterPath."""
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QPainterPath, QPolygonF

    body = QPainterPath()
    body.addRoundedRect(QRectF(cx - 8, cy - 4.5, 16, 11.5), 2.5, 2.5)
    hump = QPainterPath()
    hump.addPolygon(QPolygonF([QPointF(cx - 3.8, cy - 4), QPointF(cx - 2.3, cy - 7),
                               QPointF(cx + 2.3, cy - 7), QPointF(cx + 3.8, cy - 4)]))
    hump.closeSubpath()
    ring = QPainterPath()
    ring.addEllipse(QPointF(cx, cy + 1.2), 3.9, 3.9)
    lens = QPainterPath()
    lens.addEllipse(QPointF(cx, cy + 1.2), 2.1, 2.1)
    return body.united(hump).subtracted(ring).united(lens)


def _draw_line_glyph(p, kind: str, x: float, y: float, color: str, width: float = 1.6):
    """Stroke a ~16 px line icon centred on (x, y). No fills except tiny knobs."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen, QPolygonF

    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.save()
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    P = QPointF
    if kind == "display":
        p.drawRoundedRect(QRectF(x - 7, y - 6.5, 14, 10), 1.5, 1.5)
        p.drawLine(P(x, y + 3.5), P(x, y + 6.5))
        p.drawLine(P(x - 3.5, y + 6.5), P(x + 3.5, y + 6.5))
    elif kind == "gauge":
        cy = y + 2
        p.drawArc(QRectF(x - 7, cy - 7, 14, 14), -25 * 16, 230 * 16)
        a = math.radians(-45)
        p.drawLine(P(x, cy), P(x + 4.5 * math.cos(a), cy + 4.5 * math.sin(a)))
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(P(x, cy), 1.6, 1.6)
    elif kind == "sliders":
        for dx, ky in ((-5, 2.5), (0, -3), (5, 1)):
            p.drawLine(P(x + dx, y - 7), P(x + dx, y + 7))
            p.save()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(x + dx - 2.5, y + ky - 1.4, 5, 2.8), 1, 1)
            p.restore()
    elif kind == "speaker":
        p.drawPolygon(QPolygonF([P(x - 7, y - 2.5), P(x - 4.5, y - 2.5), P(x - 1, y - 6),
                                 P(x - 1, y + 6), P(x - 4.5, y + 2.5), P(x - 7, y + 2.5)]))
        for r in (3.5, 6.5):
            p.drawArc(QRectF(x - 0.5 - r, y - r, 2 * r, 2 * r), -45 * 16, 90 * 16)
    elif kind == "mic":
        p.drawRoundedRect(QRectF(x - 2.5, y - 7.5, 5, 9.5), 2.5, 2.5)
        p.drawArc(QRectF(x - 5.5, y - 5, 11, 9.5), 180 * 16, 180 * 16)
        p.drawLine(P(x, y + 4.5), P(x, y + 7))
        p.drawLine(P(x - 3, y + 7), P(x + 3, y + 7))
    elif kind == "micdev":
        mx = x - 3
        p.drawRoundedRect(QRectF(mx - 2.5, y - 7.5, 5, 9.5), 2.5, 2.5)
        p.drawArc(QRectF(mx - 5, y - 5, 10, 9.5), 180 * 16, 180 * 16)
        cable = QPainterPath(P(mx, y + 4.5))
        cable.lineTo(mx, y + 5.5)
        cable.quadTo(mx, y + 7.5, mx + 2.5, y + 7.5)
        cable.lineTo(x + 3.5, y + 7.5)
        cable.quadTo(x + 5.5, y + 7.5, x + 5.5, y + 5.5)
        cable.lineTo(x + 5.5, y + 1)
        p.drawPath(cable)
        p.drawRoundedRect(QRectF(x + 3.5, y - 3, 4, 4), 1, 1)
        p.drawLine(P(x + 4.7, y - 3), P(x + 4.7, y - 5.5))
        p.drawLine(P(x + 6.3, y - 3), P(x + 6.3, y - 5.5))
    elif kind == "check":
        p.drawPolyline(QPolygonF([P(x - 5.5, y + 0.5), P(x - 1.8, y + 4.2), P(x + 5.5, y - 4)]))
    elif kind == "stopsq":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(x - 4.5, y - 4.5, 9, 9), 1.5, 1.5)
    elif kind == "cross":
        p.drawLine(P(x - 4.5, y - 4.5), P(x + 4.5, y + 4.5))
        p.drawLine(P(x - 4.5, y + 4.5), P(x + 4.5, y - 4.5))
    elif kind == "fullscreen":
        # four corner brackets: everything on the screen
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            cx, cy = x + sx * 7, y + sy * 5.5
            p.drawPolyline(QPolygonF([P(cx, cy - sy * 3.5), P(cx, cy), P(cx - sx * 3.5, cy)]))
    elif kind == "window":
        # one app window: a frame with a title bar
        p.drawRoundedRect(QRectF(x - 7, y - 5.5, 14, 11), 1.5, 1.5)
        p.drawLine(P(x - 7, y - 2.2), P(x + 7, y - 2.2))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(P(x + 4.6, y - 3.9), 0.9, 0.9)
    elif kind == "back":
        p.drawLine(P(x - 6, y), P(x + 6, y))
        p.drawPolyline(QPolygonF([P(x - 1.5, y - 4.5), P(x - 6, y), P(x - 1.5, y + 4.5)]))
    elif kind == "gamepad":
        # a controller: body with two grips, a D-pad cross and two face buttons
        body = QPainterPath(P(x - 4, y - 4.5))
        body.lineTo(x + 4, y - 4.5)
        body.cubicTo(x + 7, y - 4.5, x + 8, y - 2.5, x + 8.2, y + 1.5)
        body.cubicTo(x + 8.4, y + 5, x + 7.4, y + 6.2, x + 6, y + 6.2)
        body.cubicTo(x + 4.6, y + 6.2, x + 4, y + 4.4, x + 2.6, y + 3.2)
        body.lineTo(x - 2.6, y + 3.2)
        body.cubicTo(x - 4, y + 4.4, x - 4.6, y + 6.2, x - 6, y + 6.2)
        body.cubicTo(x - 7.4, y + 6.2, x - 8.4, y + 5, x - 8.2, y + 1.5)
        body.cubicTo(x - 8, y - 2.5, x - 7, y - 4.5, x - 4, y - 4.5)
        p.drawPath(body)
        thin = QPen(pen)
        thin.setWidthF(max(1.2, width - 0.2))
        p.setPen(thin)
        p.drawLine(P(x - 4.6, y - 0.6), P(x - 1.8, y - 0.6))
        p.drawLine(P(x - 3.2, y - 2), P(x - 3.2, y + 0.8))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(P(x + 3.8, y - 1.6), 0.95, 0.95)
        p.drawEllipse(P(x + 2.4, y + 0.6), 0.95, 0.95)
    p.restore()


RES_LABELS = {"720p": "720p", "1080p": "1080p", "1440p": "1440p", "2160p": "4K", "native": "Native"}
ROW_ICONS = {"record": "fullscreen", "resolution": "display", "fps": "gauge", "quality": "sliders",
             "audio_source": "speaker", "mic": "mic", "mic_device": "micdev", "controller": "gamepad"}
RECORD_ICONS = {"screen": "fullscreen", "window": "window"}  # the Record row's icon follows its value
GLYPH_W = 16             # settings: icon column
GLYPH_GAP = 10


class _PadKey:
    """A controller action dressed as the key event ``Bar.handle_key`` expects."""

    __slots__ = ("_k",)

    def __init__(self, k):
        self._k = k

    def key(self):
        return self._k


def _build(argv=None):  # noqa: C901 - one cohesive UI builder
    from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QRectF, Qt, QTimer,
                                QVariantAnimation, Signal)
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
                                   QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

    from . import config, gamepad, ipc, quality, settings

    class Bridge(QObject):
        # Every result carries the open it belongs to (Bar.gen): a resident bar
        # drops replies that arrive after it was hidden and shown again.
        status = Signal(int, object)
        saved = Signal(int, object)
        settings = Signal(int, object)
        configured = Signal(int, object)
        control = Signal(int, str, object)
        started = Signal(int, object)
        shot = Signal(object)          # screenshot reply (the bar is already hidden)

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

    class Pill(QPushButton):
        """A button painted as a pill (no stylesheet, so its colours can ease).

        Three states are kept apart: hover (the mouse is over it), keyboard focus
        (drawn only while the keyboard / a controller is in use, see
        ``Bar.focus_visible``) and selected (a settings value). A mouse click never
        takes focus, so nothing stays lit after the pointer leaves.
        """

        def __init__(self, text="", height=BAR_HEIGHT):
            super().__init__(text)
            self.setFocusPolicy(Qt.TabFocus)
            self.setFixedHeight(height)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.setCursor(Qt.PointingHandCursor)
            self.setFont(ui_font())
            self.hovered = False
            self.visual_state = None
            self._from = self._to = None
            self._t = 1.0
            self.anim = QVariantAnimation(self)
            self.anim.setDuration(ANIM_MS)
            self.anim.setEasingCurve(QEasingCurve.OutCubic)
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.valueChanged.connect(self._tick)
            self.sync(animate=False)

        # --- what a subclass tweaks
        def rest_text(self):
            return TEXT

        def selected(self):
            return False

        # --- state
        def focus_shown(self):
            return self.hasFocus() and getattr(self.window(), "focus_visible", True)

        def target(self):
            if not self.isEnabled():
                return "disabled", (QColor(0, 0, 0, 0), QColor(DIM), 0.0)
            focus = self.focus_shown()
            if focus:
                return "focus", (QColor(PILL_ON), QColor(ON_TEXT), 1.0)
            if self.hovered:
                return "hover", (QColor(PILL_ON), QColor(ON_TEXT), 0.0)
            if self.selected():
                return "selected", (QColor(PILL_SEL), QColor(ON_TEXT), 0.0)
            return "rest", (QColor(PILL_REST), QColor(self.rest_text()), 0.0)

        @property
        def active(self):
            return self.visual_state in ("hover", "focus")

        def current(self):
            if self._to is None:
                return None
            t = self._t
            if t >= 1.0 or self._from is None:
                return self._to

            def mix(c0, c1):
                return QColor.fromRgbF(*(a + (b - a) * t for a, b in zip(c0.getRgbF(), c1.getRgbF())))
            (f0, t0, r0), (f1, t1, r1) = self._from, self._to
            return mix(f0, f1), mix(t0, t1), r0 + (r1 - r0) * t

        def sync(self, animate=True):
            state, style = self.target()
            key = (state, style[0].rgba(), style[1].rgba(), style[2])
            if key == getattr(self, "_key", None):
                return
            self._key = key
            cur = self.current()
            self.visual_state, self._to = state, style
            self.anim.stop()
            if animate and cur is not None and self.isVisible():
                self._from, self._t = cur, 0.0
                self.anim.start()
            else:
                self._from, self._t = None, 1.0
            self.update()

        def settle(self):
            self.anim.stop()
            self._t = 1.0
            self.update()

        def _tick(self, v):
            self._t = float(v)
            self.update()

        def event(self, ev):
            t = ev.type()
            if t == QEvent.Enter:
                self.hovered = True
            elif t in (QEvent.Leave, QEvent.Hide):
                self.hovered = False
            r = super().event(ev)
            if t in (QEvent.Enter, QEvent.Leave, QEvent.FocusIn, QEvent.FocusOut, QEvent.EnabledChange):
                self.sync()
            elif t in (QEvent.Hide, QEvent.Show):
                self.sync(animate=False)
            return r

        # --- painting
        def pill_rect(self):
            h = min(PILL_H, self.height())
            return QRectF(PILL_INSET, (self.height() - h) / 2, self.width() - 2 * PILL_INSET, h)

        def paintEvent(self, ev):
            style = self.current()
            if style is None:
                return
            fill, text, ring = style
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            r = self.pill_rect()
            if ring > 0.01:
                c = QColor(RING)
                c.setAlphaF(min(1.0, ring))
                p.setPen(QPen(c, 1.5))
                p.setBrush(Qt.NoBrush)
                rr = r.adjusted(-2.75, -2.75, 2.75, 2.75)
                p.drawRoundedRect(rr, rr.height() / 2, rr.height() / 2)
            if fill.alpha():
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            self.paint_content(p, r, text)
            p.end()

        def paint_content(self, p, r, color):
            p.setPen(color)
            p.setFont(self.font())
            p.drawText(r, Qt.AlignCenter, self.text())

    class Option(Pill):
        def __init__(self, index, seconds, text):
            super().__init__(text)
            self.index, self.seconds = index, seconds
            self.setFont(ui_font(tabular=True))
            self.setFixedWidth(max(OPTION_MIN_WIDTH,
                                   self.fontMetrics().horizontalAdvance(text) + 2 * (PILL_PAD + PILL_INSET)))
            self.setProperty("long", False)

        def rest_text(self):
            return DIM if self.property("long") else TEXT

        def set_long(self, long):
            if self.property("long") != long:
                self.setProperty("long", long)
                self.sync()

    class TextButton(Pill):
        def __init__(self, text, height=BAR_HEIGHT, glyph=None, quiet=False):
            super().__init__(text, height)
            self.glyph = glyph
            self.quiet = quiet
            if quiet:
                self.setObjectName("quiet")
            lead = 14 + (16 + 8 if glyph else 0)
            self.lead = lead
            self.setFixedWidth(lead + self.fontMetrics().horizontalAdvance(text) + 16 + 2 * PILL_INSET)
            self.sync(animate=False)

        def rest_text(self):
            return MUTED if getattr(self, "quiet", False) else TEXT

        def paint_content(self, p, r, color):
            if not self.glyph:
                return super().paint_content(p, r, color)
            _draw_line_glyph(p, self.glyph, r.left() + 14 + 8, r.center().y(), color.name())
            p.setPen(color)
            p.setFont(self.font())
            p.drawText(r.adjusted(self.lead, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text())

    class RowIcon(QWidget):
        """The line icon in front of a settings label; brightens while its row has focus."""

        def __init__(self, kind):
            super().__init__()
            self.kind = kind
            self.setFixedSize(GLYPH_W, ROW_H)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            row = self.parentWidget()
            w = QApplication.focusWidget()
            on = row is not None and w is not None and row.isAncestorOf(w)
            _draw_line_glyph(p, self.kind, GLYPH_W / 2, ROW_H / 2, TEXT if on else MUTED)
            p.end()

    class IconButton(Pill):
        """pause / play / stop / camera / gear in a circle, painted so it never depends on a font."""

        TIPS = {"gear": "Settings (S)", "pause": "Pause recording (P)", "play": "Resume recording (P)",
                "start": "Start recording (P)", "stop": "Stop recording", "pick": "Pick a game window",
                "shot": "Take a screenshot"}

        def __init__(self, kind):
            super().__init__("")
            self.kind = kind
            self.setObjectName("icon")
            self.setFixedSize(ICON_W, BAR_HEIGHT)
            self.setAccessibleDescription(self.TIPS[kind])
            self.setAccessibleName(self.TIPS[kind])

        def rest_text(self):
            return MUTED

        def set_kind(self, kind):
            if kind != self.kind:
                self.kind = kind
                self.setAccessibleDescription(self.TIPS[kind])
                self.setAccessibleName(self.TIPS[kind])
                self.update()

        def paint_content(self, p, r, color):
            from PySide6.QtCore import QPointF
            from PySide6.QtGui import QPolygonF

            p.setPen(Qt.NoPen)
            p.setBrush(color)
            c = r.center()
            x, y = c.x(), c.y()
            if self.kind == "gear":
                p.drawPath(_gear_path(x, y))
            elif self.kind == "pause":
                p.drawRoundedRect(QRectF(x - 5, y - 6.5, 3.5, 13), 1, 1)
                p.drawRoundedRect(QRectF(x + 1.5, y - 6.5, 3.5, 13), 1, 1)
            elif self.kind in ("play", "start", "pick"):
                p.drawPolygon(QPolygonF([QPointF(x - 3.5, y - 6.5), QPointF(x - 3.5, y + 6.5),
                                         QPointF(x + 7, y)]))
            elif self.kind == "stop":
                p.drawRoundedRect(QRectF(x - 5.5, y - 5.5, 11, 11), 1.5, 1.5)
            elif self.kind == "shot":
                p.drawPath(_camera_path(x, y))

    class SegButton(Pill):
        def __init__(self, text="", tip=None):
            super().__init__(text, ROW_H)
            self.setObjectName("seg")
            self.setProperty("sel", False)
            self.setProperty("nofit", False)
            if tip:
                self.setAccessibleDescription(tip)

        def rest_text(self):
            return DIM if self.property("nofit") else MUTED

        def selected(self):
            return bool(self.property("sel"))

        def set_sel(self, on):
            if self.property("sel") != on:
                self.setProperty("sel", on)
                self.sync()

        def set_nofit(self, on):
            if self.property("nofit") != on:
                self.setProperty("nofit", on)
                self.sync()
                self.update()

        def paint_content(self, p, r, color):
            super().paint_content(p, r, color)
            if not self.property("nofit"):
                return
            # a small accent dot after the text: this choice needs more space than is free
            tw = self.fontMetrics().horizontalAdvance(self.text())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(RED))
            p.drawEllipse(QPointF(r.center().x() + tw / 2 + 5, r.center().y() - 4), 2.25, 2.25)

    class Logo(QWidget):
        """The Momento mark (assets/logo.svg), redrawn so an install needs no file."""

        def __init__(self, size=LOGO_SIZE):
            super().__init__()
            self.setFixedSize(size, size)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.setAccessibleName("Momento")

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.scale(self.width() / 256, self.height() / 256)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#121212"))
            p.drawRoundedRect(QRectF(8, 8, 240, 240), 56, 56)
            p.setBrush(QColor("#F3EFE6"))
            p.drawEllipse(QPointF(128, 128), 84, 84)
            # the slice before "now": from 12 o'clock back to ~10 o'clock
            p.setBrush(QColor(RED))
            p.drawPie(QRectF(44, 44, 168, 168), 90 * 16, 72 * 16)
            p.setBrush(QColor("#121212"))
            p.drawEllipse(QPointF(128, 128), 11, 11)
            p.end()

    class StorageHint(QWidget):
        """Free space on the buffer's disk: a drive glyph coloured by headroom + '742 GB'."""

        def __init__(self):
            super().__init__()
            self.level = None
            self.label = ""
            self.setFont(ui_font(tabular=True))
            fm = self.fontMetrics()
            self.setFixedSize(14 + 7 + max(fm.horizontalAdvance(t) for t in ("888.8 GB", "888 MB")), BAR_HEIGHT)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.hide()

        @property
        def color(self):
            return STORAGE_COLORS.get(self.level)

        def set_storage(self, sto):
            level = _storage_level(sto)
            label = _free_label(sto.get("free")) if level else ""
            self.level, self.label = level, label
            self.setAccessibleName(f"{label} free" if label else "")
            self.setHidden(level is None)
            self.update()

        def paintEvent(self, ev):
            if not self.level:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            col = QColor(self.color)
            y = self.height() / 2
            # a tiny drive: rounded outline with an activity light
            p.setPen(QPen(col, 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(0.7, y - 4.5, 13, 9), 2.2, 2.2)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(10, y), 1.3, 1.3)
            p.setPen(QColor(RED if self.level == "short" else MUTED))
            p.setFont(self.font())
            p.drawText(QRectF(21, 0, self.width() - 21, self.height()), Qt.AlignVCenter | Qt.AlignRight,
                       self.label)
            p.end()

    class SettingRow(QWidget):
        """A label and a segmented choice. Long lists collapse to ‹ current ›."""

        def __init__(self, bar, key, title, choices, value, avail, cycle=False, extra=None):
            super().__init__()
            self.bar, self.key = bar, key
            self.extra = extra        # a button at the end of the row (Record: "Change window")
            self.values = [c[0] for c in choices]
            self.labels = [c[1] for c in choices]
            self.idx = self.values.index(value) if value in self.values else 0
            self.cycle = cycle
            self.setFixedHeight(ROW_H)
            lay = QHBoxLayout(self)
            lay.setContentsMargins(16, 0, 12, 0)
            lay.setSpacing(SEG_SPACING)
            self.icon = RowIcon(ROW_ICONS.get(key, "sliders"))
            lay.addWidget(self.icon)
            lay.addSpacing(GLYPH_GAP - SEG_SPACING)
            t = QLabel(title)
            t.setObjectName("muted")
            t.setFixedWidth(LABEL_W)
            lay.addWidget(t)
            fm = QFontMetrics(ui_font())  # the buttons' font; the row is not styled yet
            pad = 2 * (SEG_PAD + PILL_INSET)
            if cycle:
                self.prev, self.cur, self.next = SegButton("‹", "Previous"), SegButton(), SegButton("›", "Next")
                for b in (self.prev, self.next):
                    b.setFixedWidth(ARROW_W)
                    b.setFocusPolicy(Qt.NoFocus)
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
            if extra is not None:
                lay.addWidget(extra)
            self.refresh()

        @property
        def value(self):
            return self.values[self.idx]

        def refresh(self):
            if self.cycle:
                lbl = self.labels[self.idx]
                text = self.cur.fontMetrics().elidedText(lbl, Qt.ElideRight, self.cur.maximumWidth() - 2 * (SEG_PAD + PILL_INSET))
                self.cur.setText(text)
                self.cur.setAccessibleDescription(lbl if text != lbl else "")
                self.cur.set_sel(True)
            else:
                for i, b in enumerate(self.buttons):
                    b.set_sel(i == self.idx)
            if self.key == "record":
                self.icon.kind = RECORD_ICONS.get(self.value, "fullscreen")
                self.icon.update()

        def extra_shown(self):
            return self.extra is not None and not self.extra.isHidden()

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
            if self.extra_shown():
                # the row's extra button sits right of the last value
                if self.extra.hasFocus():
                    if d < 0:
                        self.focus()
                    return
                if d > 0 and not self.cycle and self.idx == len(self.values) - 1:
                    self.extra.setFocus(Qt.TabFocusReason)
                    return
            n = len(self.values)
            i = (self.idx + d) % n if self.cycle else max(0, min(n - 1, self.idx + d))
            self.select(i)

    class Bar(QWidget):
        def __init__(self):
            super().__init__()
            self.shown_at = None  # wall-clock time the bar last appeared
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
            self.resident = False     # kept loaded: hide instead of quitting
            self.gen = 0              # bumped on every show / hide of a resident bar
            self.status_at = 0.0      # when last_status was current (monotonic)
            # The time shown: status "buffered_live" (display only) at a monotonic
            # moment, ticked locally between polls while recording. Everything
            # logical (lengths, saves) uses self.buffered.
            self.live = (0.0, 0.0)
            self.live_ticking = False
            self.max_seconds = 3600.0
            self.change_btn = None    # settings: "Change window" (window mode)
            self.applied = None       # settings: the changes the last Apply sent
            self.pads = None          # game controllers while the bar is on screen
            self.pads_handle = None
            self.bridge = Bridge()
            self.bridge.status.connect(self._sig_status)
            self.bridge.saved.connect(self._sig_saved)
            self.bridge.settings.connect(self._sig_settings)
            self.bridge.configured.connect(self._sig_configured)
            self.bridge.control.connect(self._sig_control)
            self.bridge.started.connect(self._sig_started)
            self.bridge.shot.connect(self.on_shot)
            QApplication.instance().focusChanged.connect(self.on_focus_changed)
            QApplication.instance().aboutToQuit.connect(self.pads_close)  # one-shot bar: let go first
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAutoFillBackground(False)
            self.setWindowTitle("Momento")
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFont(ui_font())
            self.focus_visible = True  # opened by the hotkey: keyboard / controller first
            # Labels only: every button paints itself (see Pill).
            self.setStyleSheet(f"""
                QWidget {{ color: {TEXT}; background: transparent; }}
                QLabel#muted {{ color: {MUTED}; }}
                QLabel#dim {{ color: {DIM}; }}
                QPushButton {{ border: none; outline: none; }}
            """)

            outer = QVBoxLayout(self)
            outer.setContentsMargins(1, 1, 1, 1)
            outer.setSpacing(0)

            # Above the bar (bottom-anchored, so the bar grows upward):
            # a one-line hint while paused, or the settings rows.
            self.hintbar = QLabel(PAUSED_HINT)
            self.hintbar.setObjectName("muted")
            self.hintbar.setTextFormat(Qt.RichText)
            self.hintbar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
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
            # logo + dot + label + time + free space share one fixed-width block so no state
            # ("12:34", "Starting 0:00", "Low storage", ...) moves the clip lengths.
            head = QWidget()
            hrow = QHBoxLayout(head)
            hrow.setContentsMargins(0, 0, 0, 0)
            hrow.setSpacing(0)
            self.logo = Logo()
            hrow.addWidget(self.logo)
            hrow.addSpacing(12)
            self.dot = QLabel()
            self.dot.setFixedSize(8, 8)
            hrow.addWidget(self.dot)
            hrow.addSpacing(8)
            self.name = QLabel("")        # empty (hidden) while recording: the dot says it
            self.name.setObjectName("muted")
            nf = ui_font()
            nf.setPixelSize(13)            # a quiet secondary word next to the time
            self.name.setFont(nf)
            self.name.setContentsMargins(0, 0, 8, 0)
            hrow.addWidget(self.name)
            self.time = QLabel("0:00")
            self.time.setFont(ui_font(tabular=True))
            hrow.addWidget(self.time)
            hrow.addStretch(1)
            self.storage_hint = StorageHint()
            hrow.addWidget(self.storage_hint)
            nfm, tfm = self.name.fontMetrics(), QFontMetrics(ui_font(tabular=True))
            head.setFixedWidth(LOGO_SIZE + 12 + 8 + 8 + 2 + max(
                (nfm.horizontalAdvance(n) + 8 if n else 0) + tfm.horizontalAdvance(t)
                for n, t in (("", "00:00"), ("Paused", "00:00"), ("Starting", "00:00"),
                             ("Off", "—"), ("Error", "00:00"), ("Low storage", "00:00"),
                             ("No window", "00:00")))
                + 16 + self.storage_hint.width())
            row.addWidget(head)
            row.addSpacing(12)
            row.addWidget(divider())
            row.addSpacing(8)
            self.options = []
            for i, (secs, text) in enumerate(PRESETS):
                o = Option(i, secs, text)
                o.clicked.connect(lambda _=False, o=o: self.choose(o))
                row.addWidget(o)
                self.options.append(o)
            row.addSpacing(8)
            row.addWidget(divider())
            row.addSpacing(4)
            self.controls = [self._controls(row)]
            row.addSpacing(8)
            self.stack.addWidget(picker)

            # page 1: a single transient line (saving / saved / errors) + controls
            line = QWidget()
            lrow = QHBoxLayout(line)
            lrow.setContentsMargins(18, 0, 0, 0)
            lrow.setSpacing(0)
            self.line = QLabel("")
            self.line.setTextFormat(Qt.RichText)
            self.line.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lrow.addWidget(self.line, 1)
            lrow.addSpacing(4)
            self.controls.append(self._controls(lrow))
            lrow.addSpacing(8)
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
            self.apply_btn = TextButton("Apply", glyph="check")
            self.apply_btn.clicked.connect(self.apply_settings)
            self.back_btn = TextButton("Back", glyph="back", quiet=True)
            self.back_btn.clicked.connect(self.close_settings)
            frow.addWidget(self.apply_btn)
            frow.addWidget(self.back_btn)
            frow.addSpacing(8)
            self.stack.addWidget(foot)

            # page 3: stop confirmation
            conf = QWidget()
            crow = QHBoxLayout(conf)
            crow.setContentsMargins(18, 0, 0, 0)
            crow.setSpacing(0)
            self.confirm = QLabel(f"Stop recording?&nbsp;&nbsp;<span style='color:{MUTED}'>"
                                  "The replay history is cleared.</span>")
            self.confirm.setTextFormat(Qt.RichText)
            self.confirm.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            crow.addWidget(self.confirm, 1)
            self.stop_yes = TextButton("Stop", glyph="stopsq")
            self.stop_yes.clicked.connect(self.confirm_stop)
            self.stop_no = TextButton("Cancel", glyph="cross", quiet=True)
            self.stop_no.clicked.connect(self.cancel_confirm)
            crow.addWidget(self.stop_yes)
            crow.addWidget(self.stop_no)
            crow.addSpacing(8)
            self.stack.addWidget(conf)

            # Width is fixed to the picker so the bar never jumps between states;
            # only the height changes (upward) for the hint line and settings.
            self.bar_w = picker.sizeHint().width() + 2
            self.setFixedSize(self.bar_w, BAR_HEIGHT + 2)
            self.view = None
            self.stopped = False      # daemon up, recording stopped by the user (Stop)
            self.warn = None          # storage warning shown above the bar
            self.set_view("off", False)

            self.idle = QTimer(self)
            self.idle.setSingleShot(True)
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.idle.timeout.connect(self.on_idle)
            self.poll = QTimer(self)
            self.poll.setInterval(1000)
            self.poll.timeout.connect(self.refresh_async)
            self.ticker = QTimer(self)
            self.ticker.setInterval(TICK_MS)
            self.ticker.timeout.connect(self.on_tick)

        # Results from worker threads, dropped when they belong to an earlier open.
        def _sig_status(self, gen, st):
            if gen == self.gen:
                self.on_status(st)

        def _sig_saved(self, gen, r):
            if gen == self.gen:
                self.on_saved(r)

        def _sig_settings(self, gen, data):
            if gen == self.gen:
                self.on_settings(data)

        def _sig_configured(self, gen, r):
            if gen == self.gen:
                self.on_configured(r)

        def _sig_control(self, gen, cmd, r):
            if gen == self.gen:
                self.on_control(cmd, r)

        def _sig_started(self, gen, st):
            if gen == self.gen:
                self.on_started(st)

        def after(self, ms, fn):
            """Run ``fn`` in ``ms`` unless the bar was hidden or reopened meanwhile."""
            gen = self.gen
            QTimer.singleShot(ms, lambda: fn() if gen == self.gen else None)

        # The control buttons, left to right (also the keyboard / controller order).
        CONTROL_ORDER = ("pause", "stop", "shot", "gear")

        def _controls(self, lay):
            btns = {k: IconButton(k) for k in self.CONTROL_ORDER}
            btns["pause"].clicked.connect(self.toggle_pause)
            btns["stop"].clicked.connect(self.ask_stop)
            btns["shot"].clicked.connect(self.take_screenshot)
            btns["gear"].clicked.connect(self.open_settings)
            for k in self.CONTROL_ORDER:
                lay.addWidget(btns[k])
            return btns

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
            hint = None
            if self.mode == "clip" and not self.done and not self.saving:
                if self.warn:
                    fm = self.hintbar.fontMetrics()
                    text = fm.elidedText(self.warn, Qt.ElideRight, self.bar_w - 2 - 34)
                    hint = f"<span style='color:{RED}'>{_esc(text)}</span>"
                elif self.paused and self.running:
                    hint = PAUSED_HINT
                elif self.view == "nowindow" and self.running:
                    hint = NO_WINDOW_HINT
            if hint is not None and self.hintbar.text() != hint:
                self.hintbar.setText(hint)
            self.hintbar.setHidden(hint is None)
            if hint is not None:
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

        NAMES = {"rec": "", "paused": "Paused", "starting": "Starting", "off": "Off",
                 "error": "Error", "lowstorage": "Low storage", "nowindow": "No window"}

        def set_view(self, view, opts_on):
            """One bar for every state: only the dot, label, time and enablement change."""
            self.view = view
            running = self.running
            dot = RED if view in ("rec", "lowstorage") else MUTED if view in ("paused", "nowindow") else DIM
            self.dot.setStyleSheet(f"background: {dot}; border-radius: 4px;")
            self.dot.show()
            self.name.setText(self.NAMES[view])
            self.name.setHidden(not self.NAMES[view])
            self.dot.setAccessibleName("Recording" if view == "rec" else self.NAMES[view])
            for c in self.controls:
                pb = c["pause"]
                if view == "off" or (view == "starting" and not running):
                    pb.set_kind("start")
                    pb.setEnabled(view == "off")
                elif view in ("paused", "lowstorage"):
                    pb.set_kind("play" if view == "paused" else "start")
                    pb.setEnabled(True)
                elif view == "nowindow":
                    pb.set_kind("pick")          # play = pick a game window
                    pb.setEnabled(True)
                else:
                    pb.set_kind("pause")
                    pb.setEnabled(True)
                c["stop"].setEnabled(running and view != "off")
                c["shot"].setEnabled(view == "rec")   # a frame of the recording: only while recording
                for b in c.values():
                    b.show()
            for o in self.options:
                o.setEnabled(opts_on)
            self.online = opts_on

        def on_focus_changed(self, _old, _new):
            if self.mode != "settings":
                return
            try:
                for r in self.rows:
                    r.icon.update()  # the focused row's icon brightens
            except RuntimeError:  # rows being rebuilt / window already destroyed
                pass

        def set_time(self, text, color=TEXT):
            self.time.setText(text)
            self.time.setStyleSheet(f"color: {color};")

        # ---------------- the recording timer
        def set_live(self, st, ticking):
            """Take the time to show from a status: "buffered_live" (falls back to "buffered")."""
            v = st.get("buffered_live")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                v = st.get("buffered") or 0.0
            v = float(v)
            if ticking and self.live_ticking:
                est = self.live_seconds()
                if 0 < est - v < 1.5:
                    v = est  # a poll a moment behind the local tick: never step back
            self.max_seconds = float(st.get("max_seconds") or 3600)
            self.live = (min(v, self.max_seconds), time.monotonic())
            self.live_ticking = ticking
            if ticking and self.isVisible():
                self.ticker.start()
            elif not ticking:
                self.ticker.stop()

        def live_seconds(self):
            v, t = self.live
            if self.live_ticking:
                v += time.monotonic() - t
            return min(v, self.max_seconds)

        def on_tick(self):
            if not self.live_ticking or self.view != "rec" or self.stack.currentIndex() != 0:
                return
            text = _mmss(self.live_seconds())
            if self.time.text() != text:
                self.time.setText(text)

        # ---------------- status
        def refresh_async(self):
            if self.status_inflight or self.saving or self.done:
                return
            self.status_inflight = True
            gen = self.gen

            def work():
                self.bridge.status.emit(gen, fetch_status())
            threading.Thread(target=work, daemon=True).start()

        def on_status(self, st):
            self.status_inflight = False
            self.apply_status(st)

        def apply_status(self, st):
            self.last_status = st
            self.status_at = time.monotonic()
            if self.saving or self.done or self.mode != "clip" or self.control_busy:
                return
            if self.stopping:
                if st.get("ok"):
                    return  # still shutting down
                self.stopping = False
            was_on = self.online is True and self.stack.currentIndex() == 0
            if not st.get("ok"):
                self.running = False
                self.paused = False
                self.stopped = False
                self.buffered = 0.0
                self.set_live({}, False)
                self.warn = None
                self.storage_hint.set_storage(None)
                if not st.get("not_running", True):
                    self.online = False
                    self.show_line(f"<span style='color:{RED}'>"
                                   f"{_esc(st.get('error') or 'Cannot reach the recorder')}</span>")
                    self.relayout()
                    return
                self.set_view("off", False)
                self.set_time("—", DIM)
                view = "off"
            else:
                self.running = True
                self.stopped = st.get("state") == "stopped"
                self.paused = st.get("state") == "paused"
                self.buffered = 0.0 if self.stopped else float(st.get("buffered") or 0.0)
                if "storage" in st or st.get("state") == "no_storage":
                    self.warn = _status_warning(st)
                # else: an older daemon without storage info; keep any warning a reply gave
                self.storage_hint.set_storage(st.get("storage"))
                if self.stopped:
                    view = "off"      # the service keeps running (hotkey), recording does not
                elif st.get("state") == "no_storage":
                    view = "lowstorage"
                elif self.paused:
                    view = "paused"
                elif st.get("state") == "no_window":
                    view = "nowindow"     # window mode: the game window closed
                elif st.get("recording"):
                    view = "rec"
                elif st.get("state") == "error":
                    view = "error"
                else:
                    view = "starting"
                self.set_view(view, self.buffered > 0 and not self.stopped)
                self.set_live({} if self.stopped else st, view == "rec")
                shown = self.live_seconds()
                if view == "off":
                    self.set_time("—", DIM)
                elif view == "lowstorage":
                    # the numbers are in the warning strip right above; keep the head compact
                    self.set_time(_mmss(shown) if shown > 0 else "", MUTED)
                else:
                    self.set_time(_mmss(shown), TEXT if view in ("rec", "paused") else MUTED)
                if view == "error" and st.get("error"):
                    # Shown in the strip above the bar; the bar has no tooltips (see main()).
                    self.warn = str(st["error"])
                for o in self.options:
                    o.set_long(o.seconds > self.buffered + 0.5)
            if self.stack.currentIndex() != 0:
                self.stack.setCurrentIndex(0)
            self.relayout()
            w = QApplication.focusWidget()
            if (self.online and not was_on) or w not in self.focusables():
                self.focus_default()

        def focus_default(self):
            if self.stack.currentIndex() != 0:
                self.setFocus(Qt.OtherFocusReason)
                return
            if self.online:
                want = _last_choice()
                target = next((o for o in self.options if o.seconds == want), self.options[0])
                target.setFocus(Qt.OtherFocusReason)
                return
            c = self.controls[0]
            # low storage is fixed in settings; otherwise play (start / resume) is the next step
            target = c["gear"] if self.view == "lowstorage" else c["pause"]
            if target.isEnabled():
                target.setFocus(Qt.OtherFocusReason)
            else:
                self.setFocus(Qt.OtherFocusReason)

        # ---------------- save
        def showEvent(self, ev):
            self.shown_at = time.time()
            if self.live_ticking:
                self.ticker.start()   # the timer only runs while the bar is on screen
            self.after(0, self.pads_open)  # after the first paint: opening devices takes a few ms
            super().showEvent(ev)

        def hideEvent(self, ev):
            self.ticker.stop()
            self.pads_close()         # a hidden bar holds no controller (and no fds)
            super().hideEvent(ev)

        def choose(self, opt):
            if (self.saving or self.done or not self.online or not opt.isEnabled()
                    or self.mode != "clip" or self.control_busy):
                return
            self.saving = True
            self.idle.stop()
            _store_choice(opt.seconds)
            shown = min(opt.seconds, self.buffered) if self.buffered else opt.seconds
            self.show_line(f"Saving last {dur_label(shown)}…")
            self.relayout()
            secs = opt.seconds
            gen = self.gen
            until = self.shown_at

            def work():
                try:
                    msg = {"cmd": "save", "seconds": secs}
                    if not CAPTURE_EXCLUDED and until is not None:
                        # The bar shows up in the recording on this desktop: end the
                        # clip at the moment it was opened so it isn't in the clip.
                        msg["until"] = until
                    r = ipc.request(msg, timeout=120)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.saved.emit(gen, r)
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
            self.after(RESULT_CLOSE_MS, self.close_bar)

        # ---------------- screenshot
        def take_screenshot(self):
            """Hide the bar, then ask the daemon for a screenshot.

            The request goes out SHOT_DELAY_MS after the hide, and the daemon takes
            the first frame captured after the request, so the bar is not in the
            picture on any desktop. The bar doesn't come back to report it: the
            daemon's desktop notification does. A one-shot bar quits after the reply.
            """
            if (self.view != "rec" or self.saving or self.done or self.control_busy
                    or self.mode != "clip"):
                return
            self.done = True
            self.idle.stop()
            self.poll.stop()
            bridge = self.bridge
            if self.resident:
                self.dismiss()
            else:
                QApplication.instance().setQuitOnLastWindowClosed(False)  # quit after the reply
                self.hide()

            def work():
                try:
                    r = ipc.request({"cmd": "screenshot"}, timeout=SHOT_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                bridge.shot.emit(r)

            QTimer.singleShot(SHOT_DELAY_MS, lambda: threading.Thread(target=work, daemon=True).start())

        def on_shot(self, r):
            if r.get("ok"):
                log.info("screenshot saved: %s", r.get("path"))
            else:
                log.warning("screenshot failed: %s", r.get("error"))
            if not self.resident:
                QApplication.instance().quit()

        # ---------------- pause / resume / stop / start
        def show_storage_warning(self, text=None):
            if text is None:
                text = _status_warning(self.last_status) or _storage_warning({})
            self.warn = text
            self.relayout()

        def toggle_pause(self):
            if self.control_busy or self.saving or self.done or self.mode != "clip":
                return
            if not self.running:
                if self.view == "off":
                    self.start_recorder()
                return
            if self.view == "nowindow":
                self.pick_window()
                return
            if self.view == "lowstorage" or (self.paused and _storage_short(self.last_status) is not None):
                self.show_storage_warning()  # resuming would fail: say why instead
                return
            cmd = "resume" if self.paused or self.stopped else "pause"
            self.control_busy = True
            gen = self.gen

            def work():
                try:
                    r = ipc.request({"cmd": cmd}, timeout=30)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit(gen, cmd, r)
            threading.Thread(target=work, daemon=True).start()

        def pick_window(self):
            """Ask the daemon to open the window picker (play in "No window", "Change window").

            The bar hides once the daemon has answered, so the picker dialog can
            take the keyboard; a one-shot bar must not quit before the request is out.
            """
            if self.control_busy or self.saving or self.done:
                return
            self.control_busy = True
            gen = self.gen

            def work():
                try:
                    r = ipc.request({"cmd": "pick_window"}, timeout=30)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit(gen, "pick_window", r)
            threading.Thread(target=work, daemon=True).start()

        def ask_stop(self):
            if (not self.running or self.stopped or self.control_busy or self.saving or self.done
                    or self.mode != "clip"):
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
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.idle.start()                   # back to the normal auto-hide

        def confirm_stop(self):
            if self.mode != "confirm" or self.control_busy:
                return
            self.control_busy = True
            self.mode = "clip"
            self.warn = None
            self.show_line("Stopping…")
            self.relayout()
            gen = self.gen

            def work():
                # Stop ends recording and clears the history; the service stays up so the
                # hotkey still opens the bar. An older daemon without "stop" is quit instead.
                cmd = "stop"
                try:
                    r = ipc.request({"cmd": "stop"}, timeout=10)
                    if not r.get("ok") and "unknown command" in str(r.get("error") or ""):
                        cmd = "quit"
                        r = ipc.request({"cmd": "quit"}, timeout=10)
                except ipc.DaemonNotRunning:
                    cmd, r = "quit", {"ok": True}
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit(gen, cmd, r)
            threading.Thread(target=work, daemon=True).start()

        def on_control(self, cmd, r):
            self.control_busy = False
            if cmd == "pick_window":
                if r.get("ok"):
                    self.close_bar()            # out of the way of the picker dialog
                    return
                if self.mode == "settings":
                    fm = self.foot.fontMetrics()
                    err = fm.elidedText(str(r.get("error") or "Could not pick a window"), Qt.ElideRight,
                                        max(120, self.foot.width()))
                    self.foot.setText(f"<span style='color:{RED}'>{_esc(err)}</span>")
                    return
            if not r.get("ok"):
                if r.get("code") == "no_storage":
                    self.show_storage_warning(_storage_warning(r.get("storage") or {}, r.get("error")))
                    self.status_inflight = False
                    self.refresh_async()
                else:
                    self.show_line(f"<span style='color:{RED}'>{_esc(r.get('error') or cmd + ' failed')}</span>")
                return
            if cmd == "stop":
                self.apply_status({**(self.last_status or {}), "ok": True, "state": "stopped",
                                   "recording": False, "buffered": 0, "buffered_live": 0})
                self.idle.stop()
                self.after(STOP_CLOSE_MS, self.close_bar)  # show Off briefly, then get out of the way
            elif cmd == "quit":
                self.stopping = True
                self.running = False
                self.idle.stop()
                self.after(STOP_CLOSE_MS, self.close_bar)  # an older daemon's Stop: same quick exit
            elif cmd == "pause" and self.last_status and self.last_status.get("ok"):
                # show it right away; the next poll confirms
                self.apply_status({**self.last_status, "state": "paused", "recording": False})
            elif cmd == "resume":
                # the buffer survives a pause: footage so far stays saveable while it restarts
                self.apply_status({**(self.last_status or {}), "ok": True, "state": "starting",
                                   "recording": False})
            self.status_inflight = False
            self.refresh_async()

        def start_recorder(self):
            if self.running or self.control_busy or self.saving or self.done:
                return
            self.control_busy = True
            self.warn = None
            if self.stack.currentIndex() != 0:
                self.stack.setCurrentIndex(0)
            self.set_view("starting", False)
            self.set_time("0:00", MUTED)
            self.relayout()
            self.setFocus(Qt.OtherFocusReason)
            gen = self.gen

            def work():
                try:
                    start_daemon()
                except Exception as e:  # noqa: BLE001
                    self.bridge.started.emit(gen, {"ok": False, "not_running": False,
                                              "error": f"cannot start Momento: {e}"})
                    return
                deadline = time.monotonic() + START_TIMEOUT_S
                st = None
                while time.monotonic() < deadline:
                    st = fetch_status(timeout=1.0)
                    if st.get("ok") and (st.get("recording") or float(st.get("buffered") or 0) > 0
                                         or st.get("state") in ("error", "no_storage")):
                        break
                    time.sleep(START_POLL_S)
                if not st or not st.get("ok"):
                    st = {"ok": False, "not_running": False,
                          "error": "Momento did not start — see: journalctl --user -u momento"}
                self.bridge.started.emit(gen, st)
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
            if focus_key and page in (0, 1):
                b = self.controls[page][focus_key]
                if b.isVisible() and b.isEnabled():
                    b.setFocus(Qt.OtherFocusReason)

        # ---------------- settings
        def open_settings(self):
            if self.mode != "clip" or self.saving or self.done or self.loading_settings or self.control_busy:
                return
            self.loading_settings = True
            gen = self.gen

            def work():
                self.bridge.settings.emit(gen, fetch_settings())
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

        def clear_rows(self):
            """Delete the settings rows (rebuilt on every open of the settings)."""
            self.rows = []
            self.change_btn = None
            old = self.panel_lay.takeAt(0)
            while old is not None:
                if old.widget() is not None:
                    old.widget().hide()
                    old.widget().deleteLater()
                old = self.panel_lay.takeAt(0)

        def build_rows(self):
            self.clear_rows()
            data = self.sdata
            vals = data["values"]
            dev = data.get("devices") or {}
            outs, ins = dev.get("outputs") or [], dev.get("inputs") or []
            avail = self.bar_w - 2 - 16 - GLYPH_W - GLYPH_GAP - LABEL_W - SEG_SPACING - 12

            header = QWidget()
            header.setFixedHeight(HEADER_H)
            hl = QHBoxLayout(header)
            hl.setContentsMargins(18, 0, 18, 0)
            title = QLabel("Settings")
            hl.addWidget(title)
            hl.addStretch(1)
            self.note = QLabel("Applying restarts recording · your replay is kept" if data.get("online")
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
            self.rows = []
            if "record" in vals:
                # What to record. In window mode a "Change window" pill sits at the end of the row.
                rec = [(r, settings.RECORD_LABELS.get(r, r))
                       for r in data["choices"].get("record", list(settings.RECORD_LABELS))]
                self.change_btn = TextButton("Change window", ROW_H, quiet=True)
                self.change_btn.setAccessibleName("Change window: pick another game window")
                self.change_btn.clicked.connect(self.change_window)
                self.rows.append(SettingRow(self, "record", "Record", rec, vals["record"],
                                            avail - self.change_btn.width(), extra=self.change_btn))
            self.rows += [
                SettingRow(self, "resolution", "Resolution", res, vals["resolution"], avail),
                SettingRow(self, "fps", "Frame rate",
                           [(f, f"{f} fps") for f in data["choices"].get("fps", [60])],
                           vals.get("fps", quality.FPS), avail),
                SettingRow(self, "quality", "Quality", qual, vals["quality"], avail),
                SettingRow(self, "audio_source", "Sound", sound, vals["audio_source"], avail,
                           cycle=len(sound) - 2 > CYCLE_OVER),
                SettingRow(self, "mic", "Mic", [("off", "Off"), ("on", "On")], vals["mic"], avail),
                SettingRow(self, "mic_device", "Mic device", micdev, vals["mic_device"], avail,
                           cycle=len(micdev) - 1 > CYCLE_OVER),
            ]
            if "controller" in vals and data.get("controller_available", True):
                ctl = [("off", "Off")] + [(k, label) for k, label, _b in gamepad.CHORD_PRESETS]
                if vals["controller"] not in [c[0] for c in ctl]:
                    ctl.append((vals["controller"], settings.controller_label(vals["controller"]), True))
                self.rows.append(SettingRow(self, "controller", "Controller", ctl, vals["controller"], avail))
            for r in self.rows:
                self.panel_lay.addWidget(r)
            self.row("mic_device").setHidden(vals["mic"] != "on")
            self.update_change_btn()
            self.update_fit()

        def update_change_btn(self):
            """"Change window" shows while the daemon runs in window mode and the row still says so."""
            if self.change_btn is None:
                return
            vals = self.sdata["values"]
            on = (bool(self.sdata.get("online")) and self.running and vals.get("record") == "window"
                  and self.row("record").value == "window")
            if self.change_btn.hasFocus() and not on:
                self.row("record").focus()
            self.change_btn.setHidden(not on)

        def change_window(self):
            if self.mode != "settings" or self.apply_state in ("busy", "done"):
                return
            self.foot.setText(f"<span style='color:{MUTED}'>Opening the window picker…</span>")
            self.pick_window()

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
            cap = {"resolution": v["resolution"], "quality": v["quality"], "fps": v.get("fps", quality.FPS),
                   "bitrate_kbps": self.sdata["values"].get("bitrate", 0)}
            secs = int(self.sdata.get("max_seconds") or 3600)
            gb = quality.buffer_gb(quality.bitrate_kbps(cap), secs)
            return f"{cap['fps']} fps · ~{gb:.1f} GB for {secs // 60} min"

        # storage: the settings reply carries free space and what each combination needs
        def storage_free(self):
            sto = (self.sdata or {}).get("storage")
            if not isinstance(sto, dict):
                return None
            return float(sto.get("free") or 0) + float(sto.get("reclaimable") or 0)

        def storage_need(self, v):
            sto = (self.sdata or {}).get("storage")
            if not isinstance(sto, dict):
                return None
            need = (sto.get("required") or {}).get(f"{v['resolution']}/{v['quality']}/{v.get('fps', quality.FPS)}")
            return float(need) if need is not None else None

        def fits(self, v):
            need, free = self.storage_need(v), self.storage_free()
            return need is None or free is None or need <= free

        def current_need(self):
            sto = (self.sdata or {}).get("storage") or {}
            cur = sto.get("current")
            if cur and cur in (sto.get("required") or {}):
                return float(sto["required"][cur])
            return self.storage_need({k: self.sdata["values"].get(k) for k in ("resolution", "quality", "fps")})

        def can_apply(self, v):
            """Blocked only when the choice does not fit AND needs more than today's settings:
            stepping down must always be possible, even while space is still short."""
            if self.fits(v):
                return True
            need, cur = self.storage_need(v), self.current_need()
            return cur is not None and need is not None and need <= cur

        def update_fit(self):
            v = self.pending()
            for key in ("resolution", "quality", "fps"):
                row = self.row(key)
                if row.cycle:
                    continue
                for val, b in zip(row.values, row.buttons):
                    b.set_nofit(not self.fits({**v, key: val}))
            ok = self.can_apply(v)
            if self.apply_btn.isEnabled() != ok:
                had = self.apply_btn.hasFocus()
                self.apply_btn.setEnabled(ok)
                if had:
                    self.back_btn.setFocus(Qt.TabFocusReason)

        def update_foot(self):
            if self.apply_state is not None:
                return
            v = self.pending()
            if not self.fits(v):
                self.foot.setText(f"<span style='color:{RED}'>Needs {_gb(self.storage_need(v))} GB"
                                  f" · {_gb(self.storage_free())} GB free</span>")
            else:
                self.foot.setText(f"<span style='color:{MUTED}'>{_esc(self.estimate())}</span>")

        def on_row_changed(self, row):
            if row.key == "record":
                self.update_change_btn()
            if row.key == "mic":
                self.row("mic_device").setHidden(row.value != "on")
                self.relayout()
            if self.apply_state == "error":
                self.apply_state = None
            self.update_fit()
            self.update_foot()

        def close_settings(self, focus_key="gear"):
            if self.mode != "settings" or self.apply_state == "busy":
                return
            self.apply_state = None
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.idle.start()
            self.back_to_clip(focus_key)
            if focus_key is None:
                self.focus_default()  # after Apply: back on a clip length, not the gear
            self.refresh_async()

        def apply_settings(self):
            if self.mode != "settings" or self.apply_state in ("busy", "done"):
                return
            if not self.apply_btn.isEnabled():
                return  # the chosen combination needs more space than is free
            changes = self.changes()
            if not changes:
                self.close_settings()
                return
            self.apply_state = "busy"
            self.applied = dict(changes)
            self.idle.stop()
            self.foot.setText("Applying…")
            online, path = bool(self.sdata.get("online")), self.sdata.get("config")
            gen = self.gen

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
                self.bridge.configured.emit(gen, r)
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
            if r.get("warning") or r.get("state") == "no_storage":
                # saved, but even the new settings do not fit yet
                fm = self.foot.fontMetrics()
                msg = str(r.get("warning") or "Not enough free space")
                msg = fm.elidedText(f"Saved · {msg}", Qt.ElideRight, max(120, self.foot.width()))
                self.foot.setText(f"<span style='color:{RED}'>{_esc(msg)}</span>")
                self.last_status = {**(self.last_status or {}), "ok": True, "state": "no_storage",
                                    "recording": False, "error": r.get("warning")}
                self.after(APPLY_CLOSE_MS, self.after_apply)
                return
            # Controller settings apply at once, without restarting the recording.
            pads_only = bool(self.applied) and set(self.applied) <= set(settings.CONTROLLER_KEYS)
            if not r.get("online"):
                tail = "takes effect when Momento starts"
            elif pads_only:
                tail = "controller updated"
            elif r.get("paused"):
                tail = "applies when you resume"
            else:
                tail = "recording restarted"
            self.foot.setText(f"Saved&nbsp;<span style='color:{MUTED}'>— {tail}</span>")
            if r.get("online") and not r.get("paused") and not pads_only:
                # the recorder restarts; footage already buffered stays saveable
                self.last_status = {**(self.last_status or {}), "ok": True, "state": "starting",
                                    "recording": False}
                if (r.get("changed") or {}).get("record") == "window":
                    # the desktop's window picker opens now: get out of its way
                    self.foot.setText(f"Saved&nbsp;<span style='color:{MUTED}'>— pick the game window</span>")
                    self.after(0, self.after_apply)
                    return
            self.after(APPLY_CLOSE_MS, self.after_apply)

        def after_apply(self):
            """After Apply: back to the clip view (the state a new open starts in), then hide."""
            if self.mode == "settings" and self.apply_state == "done":
                self.apply_state = None
                self.close_settings(focus_key=None)
                self.close_bar()

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
                    (self.apply_btn if self.apply_btn.isEnabled() else self.back_btn).setFocus(Qt.TabFocusReason)
            elif k in (Qt.Key_Left, Qt.Key_Right):
                d = -1 if k == Qt.Key_Left else 1
                if i < len(rows):
                    rows[i].step(d)
                else:
                    tgt = self.apply_btn if d < 0 and self.apply_btn.isEnabled() else self.back_btn
                    tgt.setFocus(Qt.TabFocusReason)
            elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select):
                if self.back_btn.hasFocus():
                    self.close_settings()
                elif self.change_btn is not None and self.change_btn.hasFocus():
                    self.change_window()
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

        # ---------------- game controllers
        # While the bar is on screen it reads the controllers itself and (with
        # [controller] exclusive) takes them over, so the game doesn't see the
        # presses. Actions go through the same paths as the keyboard.
        PAD_KEYS = {"up": Qt.Key_Up, "down": Qt.Key_Down, "left": Qt.Key_Left, "right": Qt.Key_Right,
                    "accept": Qt.Key_Return, "back": Qt.Key_Escape}

        def pads_open(self):
            if self.pads is not None or not self.isVisible():
                return
            try:
                ctl = config.load_controller()
            except Exception:  # noqa: BLE001 - never let the controller break the bar
                log.exception("cannot read the controller settings")
                return
            if not ctl["enabled"]:
                return
            factory = PAD_FACTORY or gamepad.Gamepads
            hub = factory(navigate=True, chord=ctl["chord"], hold_ms=ctl["hold_ms"],
                          on_action=self.on_pad_action, on_chord=self.on_pad_chord)
            try:
                ok = hub.start()
            except Exception:  # noqa: BLE001
                log.exception("controller support failed to start")
                ok = False
            if not ok:
                hub.close()
                return
            self.pads = hub
            self.pads_handle = hub.attach_qt(self)
            if ctl["exclusive"]:
                hub.grab()

        def pads_close(self):
            hub, self.pads = self.pads, None
            handle, self.pads_handle = self.pads_handle, None
            try:
                if handle is not None:
                    handle.detach()
            finally:
                if hub is not None:
                    hub.close()       # ungrabs first

        def on_pad_action(self, action, repeat=False):
            if not self.isVisible():
                return
            if not self.saving and not self.done:
                self.idle.start()     # like a key press: restart the auto-hide
            self.set_focus_visible(True)
            if action in ("prev_section", "next_section"):
                self.pad_section(-1 if action == "prev_section" else 1)
                return
            k = self.PAD_KEYS.get(action)
            if action == "settings" and self.mode == "clip":
                k = Qt.Key_S          # Y / Triangle: settings
            elif action == "pause" and self.mode == "clip":
                k = Qt.Key_P          # X / Square: pause / resume (play when off)
            if k is not None:
                self.handle_key(_PadKey(k))

        def pad_section(self, d):
            """Bumpers: jump between groups (clip lengths / buttons; first row / Apply)."""
            if self.mode == "settings":
                if self.apply_state in ("busy", "done"):
                    return
                rows = self.visible_rows()
                if d < 0 and rows:
                    rows[0].focus()
                elif d > 0:
                    (self.apply_btn if self.apply_btn.isEnabled() else self.back_btn).setFocus(Qt.TabFocusReason)
            elif self.mode == "confirm":
                self.confirm_key(Qt.Key_Left)
            elif not (self.saving or self.done or self.control_busy):
                items = self.focusables()
                opts = [w for w in items if w in self.options]
                rest = [w for w in items if w not in self.options]
                if d < 0 and opts:
                    if QApplication.focusWidget() in opts:
                        opts[0].setFocus(Qt.TabFocusReason)
                    else:
                        self.focus_default()
                elif d > 0 and rest:
                    rest[0].setFocus(Qt.TabFocusReason)

        def on_pad_chord(self):
            """The controller shortcut while the bar is open: close it (it toggles).

            Only for a controller we hold: a shared one also reaches the daemon, which
            toggles the bar itself (both reacting would close and reopen it).
            """
            hub = self.pads
            if hub is None or not self.isVisible():
                return
            if hub.is_grabbed(hub.last_chord_key) or not self.running:
                self.close_bar()

        # ---------------- input
        def on_idle(self):
            if self.apply_state == "busy" or self.control_busy:
                self.idle.start()
                return
            self.request_close()

        def request_close(self):
            if self.saving:
                return  # let the save finish so the result is visible
            self.close_bar()

        def close_bar(self):
            """Esc, the idle timeout, after "Saved": hide a resident bar, quit a one-shot one."""
            if self.resident:
                self.dismiss()
            else:
                QApplication.instance().quit()

        # ---------------- resident: show / hide
        def cached_status(self):
            """The last status, with the buffer grown by the time since, if it was recording.

            A resident bar paints this at once; the real status follows a moment later.
            """
            st = self.last_status
            if not st:
                return {"ok": False, "not_running": True}
            if st.get("ok") and st.get("recording") and self.status_at:
                grown = max(0.0, time.monotonic() - self.status_at)
                cap = float(st.get("max_seconds") or 3600)
                st = {**st, "buffered": min(cap, float(st.get("buffered") or 0.0) + grown)}
                if isinstance(st.get("buffered_live"), (int, float)):
                    st["buffered_live"] = min(cap, float(st["buffered_live"]) + grown)
            return st

        def reset(self):
            """Put the bar back in the state a freshly started one opens in."""
            self.gen += 1                       # replies to the previous open are dropped
            self.idle.stop()
            self.poll.stop()
            self.ticker.stop()
            self.live_ticking = False
            self.saving = self.done = False
            self.control_busy = self.stopping = self.loading_settings = False
            self.status_inflight = False
            self.apply_state = None
            self.applied = None
            self.mode = "clip"
            self.sdata = None
            self.clear_rows()
            self.online = None
            self.running = self.paused = self.stopped = False
            self.buffered = 0.0
            self.warn = None
            self.line.setText("")
            self.storage_hint.set_storage(None)
            for o in self.options:
                o.set_long(False)
            self.stack.setCurrentIndex(0)
            self.set_view("off", False)
            self.set_time("0:00")
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.focus_visible = True           # opened by the hotkey: keyboard / controller first
            self.apply_status(self.cached_status())
            self.relayout()
            for b in self.pills():
                b.hovered = False
                b.sync(animate=False)

        def place(self):
            """Bottom centre of the primary screen (the plain-window fallback only)."""
            screen = QApplication.primaryScreen()
            if screen is not None:
                g = screen.availableGeometry()  # stays clear of a bottom taskbar
                self.move(g.x() + (g.width() - self.width()) // 2,
                          g.y() + g.height() - self.height() - BOTTOM_MARGIN)

        def present(self):
            """Show the resident bar, starting over as a fresh bar would."""
            if self.isVisible():
                self.raise_()
                self.activateWindow()
                return
            self.reset()
            if self.layered:
                _set_keyboard_interactivity(self, True)
            else:
                self.place()
            self.show()
            self.raise_()
            self.activateWindow()
            handle = self.windowHandle()
            if (QApplication.activeWindow() is not self and handle is not None
                    and QGuiApplication.focusWindow() is handle):
                # Qt still counts the window as focused from before it was hidden, so
                # showing it again activates nothing (seen with the offscreen platform;
                # a compositor sends a keyboard leave when the surface goes away).
                import warnings

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    QApplication.setActiveWindow(self)
            self.focus_default()
            self.idle.start()
            self.poll.start()
            self.refresh_async()                # the cached status painted first; this corrects it

        def dismiss(self):
            """Hide the resident bar. It lets go of the keyboard and stops polling."""
            self.gen += 1
            self.idle.stop()
            self.poll.stop()
            self.ticker.stop()
            if self.isVisible():
                self.hide()
            if self.layered:
                _set_keyboard_interactivity(self, False)
            self.clear_rows()                   # rebuilt on the next open of the settings

        def focusables(self):
            page = self.stack.currentIndex()
            if page not in (0, 1):
                return []
            c = self.controls[page]
            items = (self.options if page == 0 else []) + [c[k] for k in self.CONTROL_ORDER]
            return [w for w in items if not w.isHidden() and w.isEnabled()]

        def move_focus(self, step):
            items = self.focusables()
            if not items:
                return
            cur = next((i for i, w in enumerate(items) if w.hasFocus()), None)
            if cur is None:
                if self.stack.currentIndex() == 0:
                    self.focus_default()
                if QApplication.focusWidget() not in items:
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
                if idx < len(self.options) and self.online and self.options[idx].isEnabled():
                    self.options[idx].setFocus(Qt.ShortcutFocusReason)
                    self.choose(self.options[idx])
            elif k == Qt.Key_S:
                self.open_settings()
            elif k == Qt.Key_P:
                self.toggle_pause()
            else:
                return False
            return True

        def pills(self):
            return [b for b in self.findChildren(QPushButton) if isinstance(b, Pill)]

        def set_focus_visible(self, on):
            """Keyboard focus is drawn only while the keyboard (or a controller) is in use;
            the mouse hides it until the next key press, like :focus-visible on the web."""
            if self.focus_visible == on:
                return
            self.focus_visible = on
            for b in self.pills():
                b.sync()

        def settle(self):
            """Jump every running pill transition to its end (screenshots, tests)."""
            for b in self.pills():
                b.settle()

        def eventFilter(self, obj, ev):
            t = ev.type()
            if t in (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.TouchBegin) and self.isVisible():
                if not self.saving and not self.done:
                    self.idle.start()  # restart the idle auto-close
            if t in (QEvent.MouseButtonPress, QEvent.TouchBegin) and self.isVisible():
                self.set_focus_visible(False)
            if t == QEvent.KeyPress and self.isVisible():
                self.set_focus_visible(True)
                return self.handle_key(ev)
            return False

    return Bar, fetch_status


def _init_app(argv):
    """QApplication for the bar (layer-shell picked before it exists)."""
    use_layer_shell = _layer_shell_available()
    if use_layer_shell:
        os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"

    from PySide6.QtWidgets import QApplication

    # Our own palette and stylesheet draw everything. Use Qt's neutral Fusion
    # style rather than the desktop's (Breeze on KDE): with layer-shell as the
    # shell integration for *all* windows, Breeze's tooltip/animation handling
    # crashed the bar (null call in Breeze::Style::eventFilter on a timer).
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    app = QApplication.instance() or QApplication(["momento-overlay"] + list(argv))
    app.setStyle("Fusion")
    app.setApplicationName("Momento")
    app.setDesktopFileName("io.github.mehulchachada.Momento")
    try:
        _exclude_from_capture()
    except Exception as e:  # noqa: BLE001 - never block the bar on this
        log.debug("capture exclusion unavailable: %s", e)
    return app, use_layer_shell


# Set once the compositor has agreed to leave the bar out of screen recordings.
CAPTURE_EXCLUDED = False

_KWIN_SCRIPT = """\
// Momento: keep the clip bar out of screen recordings (KWin "exclude from capture").
function mark(w) {
  if (w && w.pid === %(pid)d && String(w.resourceClass).indexOf("python") === 0) {
    w.excludeFromCapture = true;
  }
}
workspace.stackingOrder.forEach(mark);
workspace.windowAdded.connect(mark);
"""


# KWin gained the per-window "exclude from capture" property in 6.6.0; setting
# it on an older KWin does nothing, so the bar would still be in clips.
KWIN_EXCLUDE_MIN = (6, 6)


def _kwin_script_name(pid: int) -> str:
    return f"momento-exclude-{pid}"


def _parse_kwin_version(text) -> tuple[int, int, int] | None:
    """(6, 6, 0) from KWin's supportInformation ("KWin version: 6.6.0") or
    ``kwin_wayland --version`` ("kwin 6.6.0"); None when there is no version."""
    import re

    m = re.search(r"(?:KWin version:|\bkwin(?:_wayland|_x11)?)\s+(\d+)\.(\d+)(?:\.(\d+))?",
                  str(text or ""), re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _kwin_can_exclude(version) -> bool:
    return version is not None and tuple(version[:2]) >= KWIN_EXCLUDE_MIN


def _kwin_version(bus=None) -> tuple[int, int, int] | None:
    """The running KWin's version: D-Bus supportInformation, else ``kwin_wayland --version``."""
    if bus is not None:
        try:
            from PySide6.QtDBus import QDBusInterface

            kwin = QDBusInterface("org.kde.KWin", "/KWin", "org.kde.KWin", bus)
            if kwin.isValid():
                kwin.setTimeout(2000)
                reply = kwin.call("supportInformation")
                args = reply.arguments() if reply is not None else []
                version = _parse_kwin_version(args[0]) if args else None
                if version is not None:
                    return version
        except Exception as e:  # noqa: BLE001
            log.debug("KWin supportInformation failed: %s", e)
    try:
        out = subprocess.run(["kwin_wayland", "--version"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_kwin_version(out.stdout or out.stderr)


def _exclude_from_capture() -> bool:
    """Ask KWin to hide this process's windows from screen capture.

    KWin 6.6+ has a per-window "exclude from capture" switch that also keeps
    a window out of full-monitor screencasts, which is what Momento records. The
    bar is an unnamed layer surface, so a tiny KWin script matches it by our
    PID. Returns False on other desktops (no KWin) and on older KWin, where the
    bar falls back to ending clips at the moment it was opened.
    """
    global CAPTURE_EXCLUDED
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or os.environ.get("MOMENTO_TEST_SANDBOX"):
        return False  # tests and headless runs must not touch the desktop's KWin
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except ImportError:
        return False
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    kwin = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus)
    if not kwin.isValid():
        return False
    version = _kwin_version(bus)
    if not _kwin_can_exclude(version):
        log.info("KWin %s cannot hide the bar from capture (needs %d.%d+); clips end when the bar opens",
                 ".".join(map(str, version)) if version else "(unknown version)", *KWIN_EXCLUDE_MIN)
        return False
    pid = os.getpid()
    path = RUNTIME_DIR / f"{_kwin_script_name(pid)}.js"
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(_KWIN_SCRIPT % {"pid": pid})
    except OSError as e:
        log.debug("cannot write the KWin script: %s", e)
        return False
    name = _kwin_script_name(pid)
    kwin.call("unloadScript", name)
    reply = kwin.call("loadScript", str(path), name)
    args = reply.arguments() if reply is not None else []
    if not args or not isinstance(args[0], int) or args[0] < 0:
        log.debug("KWin refused the capture-exclusion script: %s", reply.errorMessage() if reply else "")
        return False
    kwin.call("start")
    CAPTURE_EXCLUDED = True

    def cleanup():
        try:
            kwin.call("unloadScript", name)
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - best effort at exit
            pass

    import atexit

    atexit.register(cleanup)
    log.info("clip bar hidden from screen capture (KWin)")
    return True


def _create_bar(app, use_layer_shell: bool, resident: bool = False):
    """Build the bar (not shown yet) and turn it into a layer surface or a plain topmost window."""
    from PySide6.QtCore import Qt

    Bar, fetch_status = _build()
    bar = Bar()
    bar.resident = resident
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
    bar.layered = layered
    return bar


def main(argv=None) -> int:
    argv = list(argv or [])
    logging.basicConfig(level=logging.INFO, format="momento overlay: %(message)s")
    if "--resident" in argv:
        return run_resident([a for a in argv if a != "--resident"])

    if _toggle_existing():
        return 0

    from PySide6.QtCore import QTimer

    app, use_layer_shell = _init_app(argv)

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

    bar = _create_bar(app, use_layer_shell)
    layered = bar.layered
    if not layered:
        bar.place()
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

    main.window = bar  # for tests / debugging
    main.layered = layered
    try:
        return app.exec()
    finally:
        _remove_pidfile()


# --------------------------------------------------------------------------
# resident bar
# --------------------------------------------------------------------------

class ControlServer:
    """The resident bar's control socket, run on Qt's event loop.

    Same framing as the daemon's socket (one JSON object per line, one reply
    per line), so ``ipc.request(..., path=CONTROL_SOCKET)`` talks to it.
    ``handler(msg) -> dict`` runs on the GUI thread.
    """

    MAX_LINE = 1 << 16

    def __init__(self, path, handler):
        self.path = str(path)
        self.handler = handler
        self._sock = None
        self._inode = None
        self._notifier = None
        self._holder = None
        self._conns = {}  # socket -> [notifier, buffered bytes]

    def start(self) -> None:
        from PySide6.QtCore import QObject, QSocketNotifier

        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.path.exists(self.path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(self.path)
            except OSError:
                os.unlink(self.path)  # stale, from a bar that crashed
            else:
                raise RuntimeError(f"another clip bar is already listening on {self.path}")
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self._inode = os.stat(self.path).st_ino
        sock.listen(8)
        sock.setblocking(False)
        self._sock = sock
        # Notifiers get a C++ parent so they can be deleted from inside their own slot.
        self._holder = QObject()
        self._notifier = QSocketNotifier(sock.fileno(), QSocketNotifier.Read, self._holder)
        self._notifier.activated.connect(self._accept)

    def _accept(self, *_):
        from PySide6.QtCore import QSocketNotifier

        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:  # BlockingIOError: nothing left to accept
                return
            conn.setblocking(False)
            n = QSocketNotifier(conn.fileno(), QSocketNotifier.Read, self._holder)
            n.activated.connect(lambda *_, c=conn: self._read(c))
            self._conns[conn] = [n, b""]

    def _read(self, conn) -> None:
        entry = self._conns.get(conn)
        if entry is None:
            return
        try:
            data = conn.recv(4096)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(conn)
            return
        buf = entry[1] + data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise ValueError("request must be a JSON object")
            except ValueError as e:
                reply = {"ok": False, "error": f"bad request: {e}"}
            else:
                try:
                    reply = self.handler(msg)
                except Exception as e:  # noqa: BLE001
                    log.exception("control request failed")
                    reply = {"ok": False, "error": str(e) or e.__class__.__name__}
            try:
                conn.settimeout(1.0)
                conn.sendall(json.dumps(reply).encode() + b"\n")
                conn.setblocking(False)
            except OSError:
                self._drop(conn)
                return
        if len(buf) > self.MAX_LINE:
            self._drop(conn)
            return
        entry[1] = buf

    def _drop(self, conn) -> None:
        entry = self._conns.pop(conn, None)
        if entry is not None:
            entry[0].setEnabled(False)
            entry[0].deleteLater()
        try:
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        for conn in list(self._conns):
            self._drop(conn)
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        # Only remove the socket file if it is still the one we bound.
        try:
            if self._inode is not None and os.stat(self.path).st_ino == self._inode:
                os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._inode = None


def start_resident(app, use_layer_shell: bool = False, path=None):
    """Build the hidden bar and start listening for toggle / show / hide / quit.

    Returns (bar, server). The caller runs the event loop and closes the server.
    """
    bar = _create_bar(app, use_layer_shell, resident=True)
    bar.grab()  # paint once offscreen: polish, fonts and glyph caches are warm for the first show

    def handle(msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "toggle":
            if bar.isVisible():
                bar.dismiss()
            else:
                bar.present()
        elif cmd == "show":
            bar.present()
        elif cmd == "hide":
            bar.dismiss()
        elif cmd == "quit":
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, app.quit)
        elif cmd not in ("ping", "status"):
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        return {"ok": True, "visible": bar.isVisible(), "pid": os.getpid()}

    server = ControlServer(path or CONTROL_SOCKET, handle)
    server.start()
    return bar, server


def _wake_on_signals(app):
    """Run Python signal handlers promptly without a polling timer (the bar idles for hours)."""
    from PySide6.QtCore import QSocketNotifier

    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    wsock.setblocking(False)
    signal.set_wakeup_fd(wsock.fileno())

    def drain(*_):
        try:
            while rsock.recv(64):
                pass
        except OSError:
            pass
    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Read, app)
    notifier.activated.connect(drain)
    return rsock, wsock, notifier  # keep alive


def _exit_with_parent() -> None:
    """Get SIGTERM when the process that started us (the daemon) dies, even by SIGKILL.

    Under systemd the service's cgroup is cleaned up anyway; this covers a daemon
    run by hand. Linux only; elsewhere the bar simply outlives it.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def run_resident(argv=None) -> int:
    """``momento overlay --resident``: the hidden bar the daemon keeps loaded."""
    if send_resident("ping", timeout=1.0) is not None:
        log.info("a resident clip bar is already running")
        return 0
    _exit_with_parent()
    app, use_layer_shell = _init_app(argv or [])
    app.setQuitOnLastWindowClosed(False)  # hiding the bar must not end the process
    try:
        bar, server = start_resident(app, use_layer_shell)
    except (OSError, RuntimeError) as e:
        log.error("cannot start the resident clip bar: %s", e)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keep = _wake_on_signals(app)  # noqa: F841
    main.window = bar
    main.layered = bar.layered
    log.info("clip bar loaded (%s)", "layer-shell" if bar.layered else "window")
    try:
        return app.exec()
    finally:
        # Detach the wakeup socket before it is closed, so a late signal during
        # shutdown doesn't print "Bad file descriptor" into the journal.
        try:
            signal.set_wakeup_fd(-1)
        except (ValueError, OSError):
            pass
        server.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
