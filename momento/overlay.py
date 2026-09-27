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
open in the same bar, which grows upward into a row of tabs (General, Video,
Audio, Controller, Misc) over a few segmented rows; one Apply sends the
changes of every tab through the daemon's ``configure`` IPC, or straight to
the config file when the daemon is off. Resolutions taller than the recorded
picture (the daemon's ``source_size``, else the largest screen) are shown
disabled, since Momento would record them at the picture's own size.

Left of the free space sits the gallery button (key G): saved clips and
screenshots, browsed and played in a panel that opens right above the bar
(the same surface, grown upward; the bar row itself stays as it is). That
part lives in ``momento.gallery`` and is imported on the first open only, so a
resident bar that never shows it never loads QtMultimedia.

Sounds (Settings -> Audio -> Menu sounds, ``[ui] sounds``): soft UI sounds from
``momento.sfx`` for moving the focus, choosing, open / close, save, screenshot,
play / pause / stop, refusals and deletes. Only for what the user did: one
input plays at most one of them (``Bar.with_sounds``), automatic changes (the
idle hide, a window closing) play none.

Window mode (settings: Record -> Window): while stopped (the picked window
closed, or nothing picked yet) the bar says "Press play to pick a window";
play sends ``resume`` and the daemon opens the window picker itself. The bar
hides right after the reply, so the desktop's picker dialog is usable.

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

from .config import BAR_RECYCLE_EXIT, OVERLAY_SOCKET, RUNTIME_DIR
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
PILL_PAD = 10            # text padding inside a clip-length pill (every length then fits OPTION_MIN_WIDTH)
OPTION_MIN_WIDTH = 60
# Hiding on its own (every view: clip lengths, settings, the stop question, the gallery):
IDLE_HIDE_MS = 3_000     # no key, controller or mouse input on the bar for this long
LEAVE_HIDE_MS = 500      # the pointer left the bar (coming back cancels it)
# After an interaction the result is shown briefly, then the bar hides:
RESULT_CLOSE_MS = 1_200  # "Saved <file>" or a save error
REPORT_CLOSE_MS = 8_000  # Settings -> Misc -> Make a report: where the file went stays readable
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
TABS_H = 40              # settings: the tab row
TAB_PILL_H = 26          # a tab is a smaller pill than a value
TAB_PAD = 11             # text padding inside a tab
TAB_PX = 13
ROW_H = 40               # settings: a row's controls
ROW_PITCH = 52           # settings: one row, with breathing room around its controls
PANEL_PAD_T = 4
PANEL_PAD_B = 6
LABEL_W = 110            # settings: row label column
NAME_PX = 12             # the small label next to the time ("Recording Elden Ring")
SEG_PAD = 12
SEG_SPACING = 0          # pills carry their own gap (PILL_INSET)
ARROW_W = PILL_H + 2 * PILL_INSET
CYCLE_OVER = 4           # more devices than this -> ‹ current › instead of a row of names
GALLERY_IDLE_MS = 10_000   # the gallery, untouched (never while a clip plays: people watch)
GALLERY_RENEW_MS = 15_000  # the gallery keeps the controller grab alive (its watchdog gives up after 60 s)
GALLERY_HINT_MS = 6_000    # "No clips or screenshots yet": how long the strip stays up
GALLERY_EMPTY = "No clips or screenshots yet. Saved ones show up here."
# Video: the Frame rate row's note, at its end. Auto follows the recorded screen's refresh
# rate (quality.auto_fps); 120 picked by hand on a screen below 100 Hz gets FPS_NOTE.
FPS_AUTO_NOTE = "Matching your {hz} Hz screen"            # Auto records at the screen's rate
FPS_AUTO_OTHER_NOTE = "{fps} fps for your {hz} Hz screen"  # 144 Hz -> 120 fps, 75 Hz -> 60 fps
FPS_AUTO_UNKNOWN_NOTE = "Matches your screen's refresh rate"
FPS_NOTE = "Your screen is {hz} Hz \u00b7 120 fps only helps above 100 Hz"
# Full screen: the gallery isn't recorded. In the room of the stopped sentence (dot and
# time hidden); "Paused while the gallery is open" would need the bar 7 px wider.
GALLERY_PAUSED = "Paused while in the gallery"
GALLERY_JOIN = 1           # the hairline between the gallery's panel and the bar row
START_TIMEOUT_S = 10
START_POLL_S = 0.5
LOGO_SIZE = 18
ANIM_MS = 140            # pill fill / text colour transition
NOTE_FADE_MS = 140       # a note's old text fading into the new one (focus moving along a row)
NOTE_PX = TAB_PX         # the notes' size: rows, the tab row, the settings footer
NOTE_GLYPH_W = 14        # the info / warning glyph in front of a note
NOTE_GLYPH_GAP = 6
NOTE_GAP = 16            # at least this much between a row's last pill and its note
# Motion off (reduced motion): note changes land at once instead of crossfading.
ANIMATE = True

# Palette. The record dot is the only accent colour (the storage hint aside).
BG = (17, 17, 17, 240)   # #111111 at ~94 %
BORDER = "#2A2A2A"
TEXT = "#EDEDED"
MUTED = "#8A8A8A"        # labels, resting pill text, icons (not sentences: those are NOTE)
NOTE = "#B4B4B4"         # informative text: row notes, the settings header and footer
DIM = "#555555"          # disabled things only (a greyed choice); never information
RED = "#FF4D2E"
PILL_REST = "#1C1C1C"    # a resting pill: just enough to see the shape
PILL_ON = "#F2F2F2"      # hover and keyboard focus
PILL_SEL = "#CFCFCF"     # the chosen value in a settings row, when not focused
SEL_OFF = "#3A3A3A"      # a chosen value that can't apply here (a resolution above the screen)
TAB_SEL = "#262626"      # the open settings tab, when not focused
ROW_LINE = "#212121"     # the hairline between two settings rows
ON_TEXT = "#111111"      # text on a white pill
RING = "#EDEDED"         # keyboard focus ring around a white pill
GREEN = "#4CC38A"
YELLOW = "#F5C542"
WARN = YELLOW            # warnings: won't fit, can't record a format, footage dropped
# Informative text must read at WCAG AA (4.5:1) on the bar. The bar is BG over whatever
# is behind it, so the worst case is BG over white: #1F1F1F (see contrast(), the tests).
READABLE = 4.5
NOTE_COLORS = {"info": NOTE, "plain": NOTE, "warn": WARN, "error": RED, "status": TEXT}
NOTE_GLYPHS = {"info": "info", "warn": "warn", "error": "warn"}


def _luminance(color: str) -> float:
    h = color.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio of two '#RRGGBB' colours (1 to 21)."""
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def bar_background(behind: str = "#FFFFFF") -> str:
    """The bar's colour as seen over ``behind`` (BG is ~94 % opaque)."""
    h, a = behind.lstrip("#"), BG[3] / 255
    return "#" + "".join(f"{round(BG[i] * a + int(h[2 * i:2 * i + 2], 16) * (1 - a)):02X}" for i in range(3))

# Builds the bar's controller hub (momento.gamepad.Gamepads); tests swap in fakes.
PAD_FACTORY = None
# Makes the bar's sound player (momento.sfx.Sounds); tests swap in one that plays nothing.
SOUND_FACTORY = None
# Returns the largest connected screen's size in physical pixels, (w, h) or None:
# what caps the Resolution choices until the daemon knows the recorded picture's
# size. None here means "ask the kernel" (drm_screen_size); tests swap in a fake.
SCREEN_SIZE = None
# Returns the refresh rate (Hz) of the screen the bar is on, or None: what the Frame
# rate row's Auto note and the estimate use until the daemon knows the recorded
# screen's refresh (status/settings refresh_hz). None here means "ask Qt"
# (QScreen.refreshRate); tests swap in a fake.
SCREEN_REFRESH = None
DRM_ROOT = "/sys/class/drm"


def drm_screen_size(root=None):
    """The largest enabled display's native mode, from the kernel's DRM connectors.

    Qt can't be asked: with fractional scaling on Wayland it rounds the scale
    (a 1080p screen at 120% reads as 1600x900 at scale 2, i.e. "1800p"). Each
    connector's first listed mode is its preferred, native one. None if unknown.
    """
    best = None
    try:
        conns = sorted(os.scandir(root or DRM_ROOT), key=lambda e: e.name)
    except OSError:
        return None
    for conn in conns:
        try:
            base = Path(conn.path)
            if (base / "status").read_text().strip() != "connected":
                continue
            if (base / "enabled").read_text().strip() != "enabled":
                continue
            first = (base / "modes").read_text().split("\n", 1)[0].strip()
            w, h = (int(v) for v in first.split("x", 1))
        except (OSError, ValueError):
            continue
        if w > 0 and h > 0 and (best is None or (h, w) > (best[1], best[0])):
            best = (w, h)
    return best

PAUSED_HINT = "Paused · saving uses the footage so far"
# Stopped (by Stop, or the recorded window closed): what play does next.
STOPPED_TEXT = {"window": "Press play to pick a window", "screen": "Press play to record full screen"}
# What is being recorded, after "Recording" / "Paused" ("Recording Full Screen").
SUBJECT = {"screen": "Full Screen", "window": "Window"}
# The Record row's choices (the bar's own words; settings.RECORD_LABELS is the CLI's).
RECORD_TEXT = {"screen": "Full screen", "window": "Window"}
# Settings tabs: (name, keys). The daemon's settings reply ("tabs") wins; this is
# for an older daemon / settings module without them.
DEFAULT_TABS = (("General", ("record", "replay_length", "keep_history")),
                ("Video", ("resolution", "fps", "quality")),
                ("Audio", ("audio_source", "mic", "mic_device", "sounds")),
                ("Controller", ("controller",)),
                ("Misc", ("hour_warning", "instant_bar")))


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
    return "ok" if room >= 2 * need and not sto.get("low") else "tight"


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


def _low_storage(st) -> dict | None:
    """The status's storage block when the daemon says a full span doesn't fit (``low``)
    and capture still runs; None otherwise (and from a daemon without ``low``)."""
    sto = st.get("storage") if st and st.get("ok") and isinstance(st.get("storage"), dict) else None
    if sto is None or not sto.get("low") or st.get("state") == "no_storage":
        return None
    return sto


def _status_warning(st) -> str | None:
    low = _low_storage(st)
    if low is not None:
        from . import storage

        return storage.low_message(low, st.get("max_seconds") or 3600)
    sto = _storage_short(st)
    if sto is None or (st.get("state") != "no_storage" and "low" in sto):
        return None
    msg = st.get("error") if st.get("state") == "no_storage" else None
    return _storage_warning(sto, msg)


def _status_warning_soft(st) -> bool:
    """Yellow, not red: low on space for a full span, but a restart would still fit."""
    low = _low_storage(st)
    return low is not None and low.get("ok") is not False


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


def _clean_title(text) -> str | None:
    """A window title fit for one line: no control characters, runs of spaces collapsed."""
    if not isinstance(text, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text or None


def _tabs(data, fallback=DEFAULT_TABS) -> list[tuple[str, list[str]]]:
    """[(name, [keys])] from a settings reply's "tabs" ([[name, keys]] or [{name, keys}])."""
    out = []
    for t in (data.get("tabs") if isinstance(data, dict) else None) or fallback:
        if isinstance(t, dict):
            name, keys = t.get("name") or t.get("title"), t.get("keys") or t.get("settings")
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            name, keys = t
        else:
            continue
        if isinstance(name, str) and isinstance(keys, (list, tuple)):
            out.append((name, [str(k) for k in keys]))
    return out or [(n, list(k)) for n, k in fallback]


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


def _apply_layer_shell_full(widget, screen=None) -> bool:
    """The gallery's full screen view: an Overlay-layer surface anchored to every
    edge of ``screen`` (the bar's), over panels too, taking the keyboard.

    Same rules as ``_apply_layer_shell``: after winId(), before the first show().
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
    call("_ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE", 15)          # every edge
    call(KEYBOARD_SYM, 1)                                                           # Exclusive
    try:
        call("_ZN12LayerShellQt6Window16setExclusiveZoneEi", -1)                    # over panels too
    except AttributeError:
        pass
    if screen is not None:
        try:
            call("_ZN12LayerShellQt6Window22setScreenConfigurationENS0_19ScreenConfigurationE", 0)  # from QWindow
        except AttributeError:
            pass
        try:
            call("_ZN12LayerShellQt6Window9setScreenEP7QScreen",
                 shiboken6.getCppPointer(screen)[0], ctypes.c_void_p)
        except (AttributeError, TypeError):
            pass
    try:
        from PySide6.QtCore import QObject
        shiboken6.wrapInstance(lsw, QObject).setProperty("scope", "momento-gallery")
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
    elif kind == "trash":
        # a bin: the lid with its handle, the body narrowing a little, two ribs
        p.drawLine(P(x - 6, y - 4.5), P(x + 6, y - 4.5))
        p.drawPolyline(QPolygonF([P(x - 2.2, y - 4.5), P(x - 2.2, y - 6.8), P(x + 2.2, y - 6.8),
                                  P(x + 2.2, y - 4.5)]))
        p.drawPolyline(QPolygonF([P(x - 4.6, y - 4.5), P(x - 3.8, y + 6.5), P(x + 3.8, y + 6.5),
                                  P(x + 4.6, y - 4.5)]))
        p.drawLine(P(x - 1.3, y - 1.5), P(x - 1.1, y + 3.8))
        p.drawLine(P(x + 1.3, y - 1.5), P(x + 1.1, y + 3.8))
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
    elif kind == "film":
        # a strip of film: a frame with sprocket holes top and bottom
        p.drawRoundedRect(QRectF(x - 7, y - 6, 14, 12), 1.5, 1.5)
        p.drawLine(P(x - 7, y - 2.5), P(x + 7, y - 2.5))
        p.drawLine(P(x - 7, y + 2.5), P(x + 7, y + 2.5))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for dx in (-4.5, -1.5, 1.5, 4.5):
            p.drawEllipse(P(x + dx, y - 4.3), 0.7, 0.7)
            p.drawEllipse(P(x + dx, y + 4.3), 0.7, 0.7)
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
    elif kind == "history":
        # a clock face whose rim turns back on itself: footage that is kept
        r = 6.5
        p.drawArc(QRectF(x - r, y - r, 2 * r, 2 * r), 200 * 16, -290 * 16)
        a = math.radians(200)
        ex, ey = x + r * math.cos(a), y - r * math.sin(a)
        p.drawPolyline(QPolygonF([P(ex - 2.6, ey - 1.2), P(ex, ey), P(ex + 0.9, ey - 2.7)]))
        p.drawPolyline(QPolygonF([P(x, y - 3.5), P(x, y), P(x + 2.5, y + 1.6)]))
    elif kind == "timer":
        # a stopwatch: how long the replay reaches back
        cy = y + 1.2
        p.drawEllipse(P(x, cy), 6, 6)
        p.drawLine(P(x, cy - 6), P(x, cy - 7.8))
        p.drawLine(P(x - 2, y - 6.8), P(x + 2, y - 6.8))
        p.drawLine(P(x + 4.4, cy - 4.4), P(x + 5.6, cy - 5.6))
        p.drawLine(P(x, cy), P(x, cy - 3.4))
        p.drawLine(P(x, cy), P(x + 2.4, cy + 1.4))
    elif kind == "hourglass":
        p.drawLine(P(x - 5, y - 7), P(x + 5, y - 7))
        p.drawLine(P(x - 5, y + 7), P(x + 5, y + 7))
        p.drawPolyline(QPolygonF([P(x - 3.8, y - 7), P(x - 3.8, y - 4.6), P(x - 0.8, y),
                                  P(x - 3.8, y + 4.6), P(x - 3.8, y + 7)]))
        p.drawPolyline(QPolygonF([P(x + 3.8, y - 7), P(x + 3.8, y - 4.6), P(x + 0.8, y),
                                  P(x + 3.8, y + 4.6), P(x + 3.8, y + 7)]))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawPolygon(QPolygonF([P(x - 2.4, y + 5.6), P(x + 2.4, y + 5.6), P(x, y + 3)]))
    elif kind == "bolt":
        p.drawPolygon(QPolygonF([P(x + 1.6, y - 7.5), P(x - 4.6, y + 1), P(x - 0.4, y + 1),
                                 P(x - 1.6, y + 7.5), P(x + 4.6, y - 1), P(x + 0.4, y - 1)]))
    elif kind == "note":
        # an eighth note: the bar's sounds
        p.drawLine(P(x + 0.8, y + 4.2), P(x + 0.8, y - 7.2))
        flag = QPainterPath(P(x + 0.8, y - 7.2))
        flag.quadTo(x + 1.6, y - 3.6, x + 5.6, y - 2.6)
        p.drawPath(flag)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(x - 5.4, y + 1.8, 6.6, 5.0))
    elif kind == "info":
        # a thin "i" in a circle (~12 px, a note's size): information
        p.drawEllipse(P(x, y), 5.8, 5.8)
        p.drawLine(P(x, y - 0.6), P(x, y + 3.0))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(P(x, y - 3.0), 0.95, 0.95)
    elif kind == "warn":
        # a thin rounded triangle with "!": a warning (~12 px)
        p.drawPolygon(QPolygonF([P(x, y - 5.8), P(x + 6.3, y + 5.0), P(x - 6.3, y + 5.0)]))
        p.drawLine(P(x, y - 1.8), P(x, y + 1.2))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(P(x, y + 3.3), 0.9, 0.9)
    elif kind == "report":
        # a page with a folded corner and three lines of text: a report file
        p.drawPolyline(QPolygonF([P(x + 1.5, y - 7), P(x - 5.5, y - 7), P(x - 5.5, y + 7), P(x + 5.5, y + 7),
                                  P(x + 5.5, y - 3), P(x + 1.5, y - 7), P(x + 1.5, y - 3), P(x + 5.5, y - 3)]))
        for dy in (0.0, 2.8):
            p.drawLine(P(x - 2.8, y + dy), P(x + 2.8, y + dy))
        p.drawLine(P(x - 2.8, y - 2.8), P(x - 0.5, y - 2.8))
    elif kind == "gallery":
        # a media library: a photo (a mountain and a sun in a frame) on a stack
        # of them; the back frame shows only where the front one leaves room
        from PySide6.QtGui import QPainterPathStroker

        front = QRectF(x - 7.5, y - 3.5, 12.5, 10.0)
        back = QPainterPath()
        back.addRoundedRect(front.translated(2.8, -3.0), 2.2, 2.2)
        st = QPainterPathStroker()
        st.setWidth(width - 0.1)
        st.setJoinStyle(Qt.RoundJoin)
        gap = QPainterPath()
        gap.addRoundedRect(front.adjusted(-2.0, -2.0, 2.0, 2.0), 3.6, 3.6)
        p.drawRoundedRect(front, 2.2, 2.2)
        frame = QPainterPath()
        frame.addRoundedRect(front, 2.2, 2.2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawPath(st.createStroke(back).subtracted(gap))
        p.setClipPath(frame)
        left, bottom = front.left(), front.bottom()
        p.drawPolygon(QPolygonF([P(left, bottom), P(left + 4.2, bottom - 5.0), P(left + 7.0, bottom - 2.4),
                                 P(left + 8.6, bottom - 3.9), P(front.right(), bottom - 0.4),
                                 P(front.right(), bottom)]))
        p.drawEllipse(P(front.right() - 3.3, front.top() + 3.2), 1.35, 1.35)
    p.restore()


RES_LABELS = {"480p": "480p", "720p": "720p", "1080p": "1080p", "1440p": "1440p", "2160p": "4K", "native": "Native"}
ROW_ICONS = {"record": "fullscreen", "resolution": "display", "fps": "gauge", "quality": "sliders",
             "audio_source": "speaker", "mic": "mic", "mic_device": "micdev", "controller": "gamepad",
             "keep_history": "history",
             "replay_length": "timer",
             "hour_warning": "hourglass", "instant_bar": "bolt", "sounds": "note"}
# Row titles; a key a newer daemon adds gets its key as the title ("frame_pacing" -> "Frame pacing").
ROW_TITLES = {"record": "Record", "replay_length": "Replay length", "keep_history": "Keep history",
              "resolution": "Resolution",
              "fps": "Frame rate", "quality": "Quality", "audio_source": "Sound", "mic": "Mic",
              "mic_device": "Mic device", "controller": "Controller", "hour_warning": "Hour warning",
              "instant_bar": "Instant bar", "sounds": "Menu sounds"}
ROW_ICONS["format"] = "film"
ROW_TITLES["format"] = "Format"
# Settings -> Misc: not a setting, two actions (a report file for GitHub, the logs folder).
ROW_ICONS["report"] = "report"
ROW_TITLES["report"] = "Problem?"
REPORT_IDLE_NOTE = "Makes a file to attach to a GitHub issue"


def report_note(path) -> str:
    """"Saved to Home/Momento-report-… · Attach it to your GitHub issue"."""
    from .logs import shown_path

    p = Path(path)
    where = f"Home/{p.name}" if p.parent == Path.home() else shown_path(p)
    return f"Saved to {where} \u00b7 Attach it to your GitHub issue"
ON_OFF_KEYS = ("mic", "keep_history", "instant_bar", "sounds")
RECORD_ICONS = {"screen": "fullscreen", "window": "window"}  # the Record row's icon follows its value
VALUE_ICONS = {"record": RECORD_ICONS}
# Settings the daemon applies without restarting the recording (settings.LIVE_KEYS
# wins; this is for an older settings module).
LIVE_KEYS = ("controller", "replay_length", "keep_history", "hour_warning", "instant_bar", "sounds")
REPLAY_MINUTES = (15, 30, 60)   # the Replay length row, when the reply has no choices for it
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
    from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer,
                                QVariantAnimation, Signal)
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
                                   QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

    from . import config, gamepad, ipc, quality, settings, sfx

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
        reported = Signal(int, object)  # Settings -> Misc -> Make a report: the file (or the error)

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

    def largest_screen():
        """The largest enabled screen in physical pixels (see drm_screen_size), or None."""
        if SCREEN_SIZE is not None:
            return quality.source_size(SCREEN_SIZE())
        return quality.source_size(drm_screen_size())

    def local_refresh(widget=None):
        """The refresh rate (Hz) of the screen ``widget`` is on (else the primary one), or None."""
        if SCREEN_REFRESH is not None:
            return quality.refresh_hz(SCREEN_REFRESH())
        try:
            screen = (widget.screen() if widget is not None else None) or QGuiApplication.primaryScreen()
            return quality.refresh_hz(screen.refreshRate()) if screen is not None else None
        except Exception:  # noqa: BLE001 - no screen (yet)
            return None

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
                "start": "Start recording (P)", "stop": "Stop recording", "pick": "Pick a window (P)",
                "shot": "Take a screenshot", "gallery": "Gallery (G)"}

        def __init__(self, kind):
            super().__init__("")
            self.kind = kind
            self.setObjectName("icon")
            self.setFixedSize(ICON_W, BAR_HEIGHT)
            self.setAccessibleDescription(self.TIPS[kind])
            self.setAccessibleName(self.TIPS[kind])
            self.on = False           # the gallery button while its panel is open

        def rest_text(self):
            return MUTED

        def selected(self):
            return getattr(self, "on", False)

        def set_on(self, on):
            if self.on != on:
                self.on = on
                self.sync()

        # "on" (the gallery open) is drawn like a chosen value: the light fill, a dark icon

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
            elif self.kind == "gallery":
                _draw_line_glyph(p, "gallery", x, y, color.name())

    class SegButton(Pill):
        def __init__(self, text="", tip=None):
            super().__init__(text, ROW_H)
            self.setObjectName("seg")
            self.setProperty("sel", False)
            self.setProperty("nofit", False)
            self.setProperty("dim", False)   # greyed but still choosable (a format that crashed)
            if tip:
                self.setAccessibleDescription(tip)

        def rest_text(self):
            return DIM if self.property("nofit") or self.property("dim") else MUTED

        def selected(self):
            return bool(self.property("sel"))

        def set_sel(self, on):
            if self.property("sel") != on:
                self.setProperty("sel", on)
                self.sync()

        def target(self):
            if not self.isEnabled() and self.selected():
                # the saved value, which can't apply here: still shown as chosen, dimmed
                return "capped", (QColor(SEL_OFF), QColor(MUTED), 0.0)
            if (self.property("dim") and self.selected() and self.isEnabled()
                    and not self.focus_shown() and not self.hovered):
                return "capped", (QColor(SEL_OFF), QColor(MUTED), 0.0)
            return super().target()

        def set_dim(self, on):
            if self.property("dim") != on:
                self.setProperty("dim", on)
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
            # a small amber dot after the text: this choice needs more space than is free
            tw = self.fontMetrics().horizontalAdvance(self.text())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(WARN))
            p.drawEllipse(QPointF(r.center().x() + tw / 2 + 5, r.center().y() - 4), 2.25, 2.25)

    class TabButton(Pill):
        """A settings tab: a small pill. Muted text at rest, a quiet fill while open,
        white on hover / keyboard focus like every other pill."""

        def __init__(self, text):
            super().__init__(text, TABS_H)
            self.setObjectName("tab")
            self.setProperty("sel", False)
            f = ui_font()
            f.setPixelSize(TAB_PX)
            self.setFont(f)
            self.setFixedWidth(self.fontMetrics().horizontalAdvance(text) + 2 * (TAB_PAD + PILL_INSET))
            self.setAccessibleName(f"{text} settings")
            self.sync(animate=False)

        def selected(self):
            return bool(self.property("sel"))

        def set_sel(self, on):
            if self.property("sel") != on:
                self.setProperty("sel", on)
                self.sync()

        def target(self):
            state, style = super().target()
            if state == "rest":
                return state, (QColor(0, 0, 0, 0), QColor(MUTED), 0.0)
            if state == "selected":
                return state, (QColor(TAB_SEL), QColor(TEXT), 0.0)
            return state, style

        def pill_rect(self):
            h = min(TAB_PILL_H, self.height())
            return QRectF(PILL_INSET, (self.height() - h) / 2, self.width() - 2 * PILL_INSET, h)

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
            # the widest _free_label() can return: 100+ has no decimals ("742 GB"), less has one ("9.4 GB")
            self.setFixedSize(14 + 7 + max(fm.horizontalAdvance(t) for t in ("88.8 GB", "888 GB", "888 MB", "888 TB")),
                              BAR_HEIGHT)
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

    class Note(QWidget):
        """Informative text in a fixed place: a settings row's end, the tab row's right, the footer.

        ``kind``: "info" (NOTE, an "i" glyph), "warn" (amber, a warning glyph), "error"
        (red, the warning glyph), "plain" (NOTE, no glyph), "status" (TEXT, no glyph).
        ``lead``: a bright word before the text ("Saved — recording restarted").
        Too long for the width: it wraps (up to ``lines`` lines), then ends in "…" with
        the whole text as its tooltip. It never grows past the width it is given, so
        nothing next to it moves. A change crossfades (NOTE_FADE_MS; at once with
        ANIMATE off); ``text()`` is the new text right away.
        """

        def __init__(self, align=Qt.AlignRight, lines=1, px=NOTE_PX):
            super().__init__()
            self.align, self.lines = align, lines
            f = ui_font()
            f.setPixelSize(px)
            self.setFont(f)
            self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.state = ("", "info", "")        # (text, kind, lead)
            self.old = None                      # the state fading out
            self._t = 1.0
            self.elided = False
            self.anim = QVariantAnimation(self)
            self.anim.setDuration(NOTE_FADE_MS)
            self.anim.setEasingCurve(QEasingCurve.InOutQuad)
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.valueChanged.connect(self._tick)
            self.anim.finished.connect(self.settle)

        def fix_width(self, w):
            """A fixed room (a row's end, the tab row): the layout keeps exactly this much."""
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            self.setFixedWidth(max(0, int(w)))

        # --- what the rest of the bar reads
        def text(self):
            text, _kind, lead = self.state
            return f"{lead} — {text}" if lead and text else lead or text

        @property
        def kind(self):
            return self.state[1] if self.text() else None

        def set(self, text, kind="info", lead="", animate=True):
            new = (str(text or ""), kind if kind in NOTE_COLORS else "info", str(lead or ""))
            if new == self.state:
                return
            shown = self.state if self._t >= 1.0 or self.old is None else (self.state if self._t >= 0.5 else self.old)
            self.state = new
            self.anim.stop()
            if animate and ANIMATE and self.isVisible() and (shown[0] or shown[2]):
                self.old, self._t = shown, 0.0
                self.anim.start()
            else:
                self.old, self._t = None, 1.0
            self.setAccessibleName(self.text())
            self._fit()
            self.update()

        def settle(self):
            self.anim.stop()
            self.old, self._t = None, 1.0
            self.update()

        def _tick(self, v):
            self._t = float(v)
            self.update()

        # --- layout
        def glyph(self, kind):
            return NOTE_GLYPHS.get(kind)

        def text_room(self, kind, lead=""):
            room = self.width() - (NOTE_GLYPH_W + NOTE_GLYPH_GAP if self.glyph(kind) else 0)
            if lead:
                room -= self.fontMetrics().horizontalAdvance(lead + " ")
            return max(0, room)

        def layout_lines(self, state):
            """(lines, elided) for ``state`` in the current width."""
            text, kind, lead = state
            fm, room = self.fontMetrics(), self.text_room(kind, lead)
            if lead and text:
                text = f"— {text}"
            if not text:
                return [], False
            if fm.horizontalAdvance(text) <= room:
                return [text], False
            words, lines = text.split(" "), []
            while words and len(lines) < self.lines - 1:
                line = words.pop(0)
                while words and fm.horizontalAdvance(f"{line} {words[0]}") <= room:
                    line += " " + words.pop(0)
                if fm.horizontalAdvance(line) > room:        # one word longer than the room
                    words.insert(0, line)
                    break
                lines.append(line)
            rest = " ".join(words)
            if not rest:
                return lines, False
            last = fm.elidedText(rest, Qt.ElideRight, room)
            lines.append(last)
            return lines, last != rest

        def _fit(self):
            _lines, self.elided = self.layout_lines(self.state)
            self.setToolTip(self.text() if self.elided else "")

        def resizeEvent(self, ev):
            super().resizeEvent(ev)
            self._fit()

        def sizeHint(self):
            fm = self.fontMetrics()
            text, kind, lead = self.state
            w = fm.horizontalAdvance(self.text()) + (NOTE_GLYPH_W + NOTE_GLYPH_GAP if self.glyph(kind) else 0)
            return QSize(w, fm.height() * self.lines)

        # --- painting
        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            if self.old is not None and self._t < 1.0:
                self.paint_state(p, self.old, 1.0 - self._t)
                self.paint_state(p, self.state, self._t)
            else:
                self.paint_state(p, self.state, 1.0)
            p.end()

        def paint_state(self, p, state, opacity):
            text, kind, lead = state
            lines, _elided = self.layout_lines(state)
            if not lines and not lead:
                return
            p.save()
            p.setOpacity(opacity)
            p.setFont(self.font())
            fm = self.fontMetrics()
            lh = fm.height()
            top = (self.height() - lh * max(1, len(lines))) / 2
            color = QColor(NOTE_COLORS.get(kind, NOTE))
            glyph = self.glyph(kind)
            lead_w = fm.horizontalAdvance(lead + " ") if lead else 0
            first_w = lead_w + (fm.horizontalAdvance(lines[0]) if lines else 0)
            if self.align & Qt.AlignRight:
                x0 = self.width() - first_w
            else:
                x0 = NOTE_GLYPH_W + NOTE_GLYPH_GAP if glyph else 0
            if glyph:
                _draw_line_glyph(p, glyph, x0 - NOTE_GLYPH_GAP - NOTE_GLYPH_W / 2, top + lh / 2,
                                 color.name(), 1.3)
            if lead:
                p.setPen(QColor(TEXT))
                p.drawText(QRectF(x0, top, lead_w + 2, lh), Qt.AlignVCenter | Qt.AlignLeft, lead)
            p.setPen(color)
            for i, line in enumerate(lines):
                y = top + i * lh
                if self.align & Qt.AlignRight:
                    r = QRectF(0, y, self.width(), lh)
                    p.drawText(r, Qt.AlignVCenter | Qt.AlignRight, line)
                else:
                    r = QRectF(x0 + (lead_w if i == 0 else 0), y, self.width(), lh)
                    p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, line)
            p.restore()

    class SettingRow(QWidget):
        """A label and a segmented choice. Long lists collapse to ‹ current ›."""

        def __init__(self, bar, key, title, choices, value, avail, cycle=False, disabled=()):
            super().__init__()
            self.bar, self.key = bar, key
            self.tab = 0              # index of the settings tab the row is on
            self.values = [c[0] for c in choices]
            self.labels = [c[1] for c in choices]
            self.idx = self.values.index(value) if value in self.values else 0
            self.cycle = cycle
            # Values shown but not choosable (greyed, skipped by keys and controllers).
            # The saved value may be one of them: it stays selected, dimmed.
            self.disabled = set() if cycle else set(disabled)
            self.setFixedHeight(ROW_PITCH)
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
                used = self.prev.width() + self.cur.width() + self.next.width()
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
                    b.setEnabled(self.values[i] not in self.disabled)
                    lay.addWidget(b)
                    self.buttons.append(b)
                used = sum(b.width() for b in self.buttons) + SEG_SPACING * (len(self.buttons) - 1)
            lay.addStretch(1)
            # A short note at the row's end (Resolution: "Your screen is 1080p"), centred
            # with the pills. It has the room the pills leave (two lines, then "…").
            self.note = Note(Qt.AlignRight, lines=2)
            self.note.fix_width(avail - used - NOTE_GAP - 6)
            lay.addWidget(self.note)
            lay.addSpacing(6)                # ends where the tab row's note does
            self.refresh()

        @property
        def value(self):
            return self.values[self.idx]

        def has_divider(self):
            """A hairline above every row of a tab but its first (visible) one."""
            if self.isHidden():
                return False
            first = next((r for r in self.bar.rows if r.tab == self.tab and not r.isHidden()), None)
            return first is not None and first is not self

        def paintEvent(self, ev):
            if not self.has_divider():
                return
            p = QPainter(self)
            p.setPen(QPen(QColor(ROW_LINE), 1))
            p.drawLine(16, 0, self.width() - 12, 0)   # from the icon column to the row's end
            p.end()

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
            icons = VALUE_ICONS.get(self.key)
            if icons:
                self.icon.kind = icons.get(self.value, ROW_ICONS.get(self.key, "sliders"))
                self.icon.update()

        def set_note(self, text, kind="info"):
            self.note.set(text, kind)

        def enabled(self, i):
            return self.values[i] not in self.disabled

        def forced(self):
            """Send this row's value on Apply even though it is the saved one (a retry)."""
            return False

        def focus(self):
            if self.cycle:
                self.cur.setFocus(Qt.TabFocusReason)
                return
            i = self.idx
            if not self.enabled(i):
                # the saved value can't be chosen here: focus the nearest choice that can
                near = [j for j in range(len(self.values)) if self.enabled(j)]
                if not near:
                    return
                i = min(near, key=lambda j: (abs(j - self.idx), j))
            self.buttons[i].setFocus(Qt.TabFocusReason)

        def select(self, i):
            changed = i != self.idx
            self.idx = i
            self.refresh()
            self.focus()
            if changed:
                self.bar.on_row_changed(self)
                self.bar.sound("select")     # after: Sounds -> On is heard at once

        def step(self, d):
            n = len(self.values)
            if self.cycle:
                self.select((self.idx + d) % n)
                return
            # From the chosen value, even one that can't apply here (focus then sits on
            # the nearest choice that can, and the step toward it lands there).
            i = self.idx + d
            while 0 <= i < n and not self.enabled(i):
                i += d                                   # disabled choices are skipped
            if 0 <= i < n:
                self.select(i)
            else:
                self.focus()                             # at the end: stay

    class FormatRow(SettingRow):
        """Settings -> Video -> Format: Auto / H.264 / H.265 / AV1.

        Formats this machine can't record (the settings reply's ``format_allowed``)
        are disabled. Formats whose start crashed Momento here (``format_crashed``)
        are greyed but still choosable: picking one again retries it, even when it
        is the saved one. The note at the row's end says what the focused choice
        means for the clips; Auto says what it records in here ("Recording in H.264
        on this PC"). With no focus in the row: why a choice is greyed or disabled,
        or else what the selected one means (Auto: the same "Recording in" line).
        """

        AUTO_HINT = "Picks a format your PC records well"   # Auto's pick not known

        def __init__(self, bar, key, title, data, avail):
            from . import codecs

            vals, choices = data["values"], data.get("choices") or {}
            opts = list(choices.get("format") or codecs.CHOICES)
            value = vals.get("format", "auto")
            if value not in opts:
                opts.append(value)
            allowed = data.get("format_allowed")
            allowed = opts if not isinstance(allowed, list) else allowed
            unavailable = [f for f in opts if f != "auto" and f not in allowed]
            # What Auto records in here: the daemon's format_effective while Auto is the
            # saved format (what is really recorded), else its format_auto.
            picked = data.get("format_auto")
            if value == "auto" and data.get("format_effective"):
                picked = data["format_effective"]
            crashed = data.get("format_crashed")
            crashed = [f for f in crashed if f in opts and f not in unavailable] if isinstance(crashed, list) else []
            labels = [(f, codecs.label(f)) for f in opts]
            self.crashed, self.retry = crashed, None
            super().__init__(bar, key, title, labels, value, avail, disabled=unavailable)
            self.codecs, self.picked, self.unavailable, self.focused = codecs, picked, unavailable, None
            for b in self.buttons:
                b.installEventFilter(self)
            self.update_dim()
            self.update_note()

        def update_dim(self):
            for b, v in zip(self.buttons, self.values):
                b.set_dim(v in self.crashed and v != self.retry)

        def select(self, i):
            # Picking a format that crashed here asks to retry it, even the saved one.
            self.retry = self.values[i] if self.values[i] in self.crashed else None
            self.update_dim()
            if self.retry is not None and i == self.idx:
                self.refresh()
                self.focus()
                self.bar.on_row_changed(self)
                self.bar.sound("select")
                return
            super().select(i)

        def forced(self):
            return self.retry is not None and self.retry == self.value

        def auto_note(self):
            if self.picked in self.codecs.LABELS and self.picked != "auto":
                return f"Recording in {self.codecs.label(self.picked)} on this PC"
            return self.AUTO_HINT

        def hint(self, fmt):
            if fmt == "auto":
                return self.auto_note()
            if fmt in self.unavailable:
                return self.codecs.unavailable_message([fmt])
            if fmt in self.crashed and fmt != self.retry:
                return self.codecs.crashed_note([fmt])
            return self.codecs.HINTS.get(fmt, "")

        def update_note(self):
            fmt = self.focused if self.focused is not None else self.value
            crashed = [f for f in self.crashed if f != self.retry]
            if self.focused is None and (crashed or self.unavailable):
                # why a chip is greyed, while the row has no focus
                if crashed:
                    self.set_note(self.codecs.crashed_note(crashed), "warn")
                else:
                    self.set_note(self.codecs.unavailable_message(self.unavailable), "warn")
                return
            warn = fmt in self.unavailable or fmt in crashed
            self.set_note(self.hint(fmt), "warn" if warn else "info")

        def refresh(self):
            super().refresh()
            if hasattr(self, "focused"):
                self.update_note()

        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.FocusIn and obj in self.buttons:
                self.focused = self.values[self.buttons.index(obj)]
                self.update_note()
            elif ev.type() == QEvent.FocusOut and obj in self.buttons:
                self.focused = None
                self.update_note()
            return False

    class ReportRow(SettingRow):
        """Settings -> Misc -> "Problem?": Make a report · Open logs. Two buttons, not a
        setting: nothing here goes to Apply (``action``), neither pill shows as chosen, and
        Enter / a click / the controller's A runs the focused one. The note at the row's
        end says what the report is for, then where it was saved."""

        action = True

        def __init__(self, bar, avail):
            super().__init__(bar, "report", ROW_TITLES["report"],
                             [("report", "Make a report"), ("logs", "Open logs")], "report", avail)
            self.set_note(REPORT_IDLE_NOTE)

        def refresh(self):
            for b in self.buttons:
                b.set_sel(False)

        def select(self, i):
            self.idx = i
            self.focus()
            self.activate()

        def step(self, d):
            self.idx = max(0, min(len(self.values) - 1, self.idx + d))
            self.focus()

        def activate(self):
            self.bar.sound("select")
            if self.values[self.idx] == "logs":
                self.bar.open_logs(self)
            else:
                self.bar.make_report(self)

    class Bar(QWidget):
        def __init__(self):
            super().__init__()
            self.saving = False
            self.done = False
            self.online = None
            self.running = False      # daemon reachable
            self.paused = False
            self.pause_reason = None  # status pause_reason: "gallery" while the gallery holds it
            # The pause this bar asked for while its gallery is open (Full screen):
            # None | "asked" | "held". Its requests go out in order on one worker.
            self.gallery_pause = None
            self._gallery_jobs = None
            self.buffered = 0.0
            # From the status: what is recorded ("screen" | "window"), the window's
            # title, and whether a stop keeps the footage (keep_history).
            self.target = "screen"
            self.target_name = None
            self.keep_history = False
            self.resume_picks = False  # play from stopped in window mode: the picker opens
            self.status_inflight = False
            self.last_status = None
            self.mode = "clip"        # clip | settings | confirm | gallery
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
            # The replay length (status max_seconds): longer clip lengths are disabled.
            self.max_clip = 3600.0
            self.tab_btns = []        # settings: the tab row
            self.tab_names = []
            self.tabstack = None      # settings: one page of rows per tab
            self.tab = 0
            self.panel_rows = 0       # rows of the tallest tab: the panel keeps that height
            self.last_tab = None      # the tab settings reopen on (for as long as the process lives)
            self.applied = None       # settings: the changes the last Apply sent
            self.pads = None          # game controllers while the bar is on screen
            self.pads_handle = None
            self.pad_used = None      # the controller last in use, kept across opens (its hints)
            self.gallery = None       # momento.gallery.Gallery, built on the first open
            self.gallery_hint = None  # "No clips or screenshots yet" in the strip above the bar
            self.recycle = False      # the gallery was used: a resident bar exits once hidden
            self.bridge = Bridge()
            self.bridge.status.connect(self._sig_status)
            self.bridge.saved.connect(self._sig_saved)
            self.bridge.settings.connect(self._sig_settings)
            self.bridge.configured.connect(self._sig_configured)
            self.bridge.control.connect(self._sig_control)
            self.bridge.started.connect(self._sig_started)
            self.bridge.shot.connect(self.on_shot)
            self.bridge.reported.connect(self._sig_reported)
            self.reporting = False    # Make a report runs (the bar doesn't hide meanwhile)
            self.sounds = None        # momento.sfx.Sounds, made on the first show
            self.sounds_on = True     # [ui] sounds, read on every show
            self._sound_batch = None  # the sounds the input being handled asked for
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
                QPushButton {{ border: none; outline: none; }}
            """)

            outer = QVBoxLayout(self)
            outer.setContentsMargins(1, 1, 1, 1)
            outer.setSpacing(0)

            # The gallery (momento/gallery.py builds its panel in here on its first open):
            # the bar grows upward into it, like settings: one shape, rounded only at its
            # very top and bottom, the panel and the bar row apart by a hairline like the
            # bar's other dividers. One surface, so the keyboard, the pointer's leave and
            # the layer-shell anchor stay as they are.
            self.gallery_host = QWidget()
            gl = QVBoxLayout(self.gallery_host)
            gl.setContentsMargins(0, 0, 0, 0)
            gl.setSpacing(0)
            self.gallery_host.hide()
            outer.addWidget(self.gallery_host)
            self.gallery_join = QFrame()
            self.gallery_join.setFixedHeight(GALLERY_JOIN)
            self.gallery_join.setStyleSheet(f"background: {BORDER}; margin: 0 12px;")
            self.gallery_join.hide()
            outer.addWidget(self.gallery_join)

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
            # the dot and the gap after it hide together (stopped: the sentence says it all)
            self.dotbox = QWidget()
            self.dotbox.setFixedSize(16, 8)
            dl = QHBoxLayout(self.dotbox)
            dl.setContentsMargins(0, 0, 8, 0)
            self.dot = QLabel()
            self.dot.setFixedSize(8, 8)
            dl.addWidget(self.dot)
            hrow.addWidget(self.dotbox)
            self.name = QLabel("")        # "Recording Elden Ring", "Paused Full Screen", ...
            self.name.setObjectName("muted")
            self.name.setTextFormat(Qt.PlainText)   # a window title is never markup
            nf = ui_font()
            nf.setPixelSize(NAME_PX)       # a quiet secondary label next to the time
            self.name.setFont(nf)
            self.name.setContentsMargins(0, 0, 8, 0)
            hrow.addWidget(self.name)
            self.time = QLabel("0:00")
            self.time.setFont(ui_font(tabular=True))
            hrow.addWidget(self.time)
            hrow.addStretch(1)
            # the gallery: saved clips and screenshots, played right here (key G)
            self.gallery_btn = IconButton("gallery")
            self.gallery_btn.clicked.connect(self.open_gallery)
            hrow.addWidget(self.gallery_btn)
            hrow.addSpacing(4)
            self.storage_hint = StorageHint()
            hrow.addWidget(self.storage_hint)
            nfm, tfm = self.name.fontMetrics(), QFontMetrics(ui_font(tabular=True))
            # The label's room next to the time: "Recording Full Screen" fits whole, a
            # window title is elided to it. Stopped has no dot and no time, so its
            # sentence may use their room as well.
            self.name_w = nfm.horizontalAdvance(f"Recording {SUBJECT['screen']}")
            block = max([self.name_w + 8 + tfm.horizontalAdvance("00:00"),
                         max(nfm.horizontalAdvance(t) for t in STOPPED_TEXT.values()) + 8 - 16]
                        + [nfm.horizontalAdvance(n) + 8 + tfm.horizontalAdvance(t)
                           for n, t in (("Starting", "00:00"), ("Off", "—"), ("Error", "00:00"),
                                        ("Low storage", "00:00"))])
            # the room of a sentence (stopped, the gallery's pause; the name's margin aside)
            self.stopped_w = block + 16 - 8
            head.setFixedWidth(LOGO_SIZE + 12 + 16 + block + 12 + ICON_W + 4 + self.storage_hint.width())
            row.addWidget(head)
            row.addSpacing(10)
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
            # the estimate, a warning, "Applying…", "Saved — …" (NOTE_PX, like the gallery's meta line)
            self.foot = Note(Qt.AlignLeft, lines=1)
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
            self.confirm = QLabel(self.confirm_text())   # set again on every ask (keep_history)
            self.confirm.setTextFormat(Qt.PlainText)
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
            self.soft_warn = None     # the warning apply_status shows in yellow (still recording fine)
            self.set_view("off", False)

            self.idle = QTimer(self)
            self.idle.setSingleShot(True)
            self.idle.setInterval(IDLE_HIDE_MS)
            self.idle.timeout.connect(self.on_idle)
            self.leave = QTimer(self)           # the pointer left the bar
            self.leave.setSingleShot(True)
            self.leave.setInterval(LEAVE_HIDE_MS)
            self.leave.timeout.connect(self.on_leave)
            self.setMouseTracking(True)         # moving the pointer over the bar counts as use
            self.track_mouse(self)
            self.poll = QTimer(self)
            self.poll.setInterval(1000)
            self.poll.timeout.connect(self.refresh_async)
            self.ticker = QTimer(self)
            self.ticker.setInterval(TICK_MS)
            self.ticker.timeout.connect(self.on_tick)
            self.pad_renew = QTimer(self)       # the gallery keeps a grabbed controller
            self.pad_renew.setInterval(GALLERY_RENEW_MS)
            self.pad_renew.timeout.connect(self.renew_pads)

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

        def _sig_reported(self, gen, r):
            self.reporting = False
            if r.get("ok"):
                from . import logs

                logs.open_folder(Path(r["path"]).parent)     # asked for: shown even if the bar hid
            if gen == self.gen:
                self.on_reported(r)

        # ---- Settings -> Misc -> Problem?
        def make_report(self, row):
            """The same report as `momento report`, written off the UI thread."""
            if self.reporting:
                return
            self.reporting = True
            row.set_note("Making the report\u2026", "status")
            self.idle.stop()
            gen = self.gen

            def work():
                from . import report

                try:
                    r = {"ok": True, "path": str(report.write())}
                    log.info("report saved: %s", r["path"])
                except Exception as e:  # noqa: BLE001
                    log.exception("report failed")
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.reported.emit(gen, r)
            threading.Thread(target=work, name="report", daemon=True).start()

        def on_reported(self, r):
            row = self.row("report") if self.mode == "settings" else None
            if row is None:
                return
            if r.get("ok"):
                row.set_note(report_note(r["path"]), "info")
            else:
                row.set_note(f"Couldn't make the report: {r.get('error')}", "error")
            self.idle.setInterval(REPORT_CLOSE_MS)
            self.idle.start()

        def open_logs(self, row):
            from . import logs

            folder = logs.log_dir()
            try:
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                pass
            if logs.open_folder(folder):
                row.set_note(f"Opened {logs.shown_path(folder)}", "info")
            else:
                row.set_note(f"Logs are in {logs.shown_path(folder)}", "info")

        def after(self, ms, fn):
            """Run ``fn`` in ``ms`` unless the bar was hidden or reopened meanwhile."""
            gen = self.gen
            QTimer.singleShot(ms, lambda: fn() if gen == self.gen else None)

        # ---------------- sounds (momento.sfx; Settings -> Audio -> Menu sounds)
        def sounds_start(self):
            """On every show: read [ui] sounds; the first show makes the player, which
            loads its sounds in the background (never on this thread)."""
            self.sounds_on = config.load_bar_sounds()
            if self.sounds is not None:
                return
            try:
                self.sounds = (SOUND_FACTORY or sfx.Sounds)()
                self.sounds.warm()
            except Exception:  # noqa: BLE001 - sounds never break the bar
                log.exception("bar sounds unavailable")
                self.sounds = None

        def sounds_close(self):
            """Before the process exits (recycle, quit): a sound playing may finish, then
            the player and its sounds are freed."""
            s, self.sounds = self.sounds, None
            if s is not None:
                try:
                    s.wait()
                    s.free()
                except Exception:  # noqa: BLE001
                    log.exception("bar sounds: cleanup failed")

        def sounds_wanted(self):
            """The Sounds setting; in settings, what its row says right now, so turning it
            on is heard at once and off is silent at once (Back returns to the saved one)."""
            row = self.row("sounds") if self.mode == "settings" else None
            return row.value == "on" if row is not None else self.sounds_on

        def sound(self, name):
            """Play one of momento.sfx.NAMES for something the user did. Inside
            ``with_sounds`` it is collected instead, and one sound plays at the end."""
            if self._sound_batch is not None:
                self._sound_batch.append(name)
                return
            s = self.sounds
            if s is None:
                return
            try:
                s.enabled = self.sounds_wanted()
                s.play(name)
            except Exception:  # noqa: BLE001 - sounds never break the bar
                log.exception("bar sound %s failed", name)

        def with_sounds(self, fn, *args):
            """Handle one input (a key, a controller action): of the sounds it asked for
            the most telling plays (sfx.first); none, and the focus moved: "move"."""
            if self._sound_batch is not None:
                return fn(*args)
            self._sound_batch = []
            before = QApplication.focusWidget()
            try:
                return fn(*args)
            finally:
                asked, self._sound_batch = self._sound_batch or [], None
                name = sfx.first(asked)
                w = QApplication.focusWidget()
                if name is None and w is not before and w is not None and w is not self and w.isVisible():
                    name = "move"
                if name is not None:
                    self.sound(name)

        def click_tab(self, i):
            """A settings tab clicked."""
            self.sound("select")
            self.switch_tab(i, None)

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
            # one shape, the gallery's panel (when open) included: it grows with the bar
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.drawRoundedRect(r, 10, 10)
            p.end()

        # ---------------- layout
        def relayout(self):
            top = 0
            hint = None
            if self.mode == "clip" and not self.done and not self.saving:
                if self.gallery_hint:
                    hint = _esc(self.gallery_hint)
                elif self.warn:
                    fm = self.hintbar.fontMetrics()
                    text = fm.elidedText(self.warn, Qt.ElideRight, self.bar_w - 2 - 34)
                    color = YELLOW if self.warn == self.soft_warn else RED
                    hint = f"<span style='color:{color}'>{_esc(text)}</span>"
                elif self.paused and self.running:
                    hint = PAUSED_HINT
            if hint is not None and self.hintbar.text() != hint:
                self.hintbar.setText(hint)
            self.hintbar.setHidden(hint is None)
            if hint is not None:
                top += HINT_H
            if self.mode == "settings":
                # every tab gets the tallest tab's height, so switching tabs never moves the bar
                ph = PANEL_PAD_T + TABS_H + self.panel_rows * ROW_PITCH + PANEL_PAD_B
                self.panel.setFixedHeight(ph)
                self.panel.show()
                top += ph
            else:
                self.panel.hide()
            self.sep.setHidden(top == 0)
            h = BAR_HEIGHT + 2 + top + (1 if top else 0)
            g = self.gallery
            if g is not None and (self.mode == "gallery" or g.closing):
                gh = g.shown_height()                 # grows / folds with the gallery's motion
                self.gallery_host.setFixedHeight(gh)
                self.gallery_host.show()
                self.gallery_join.setHidden(gh <= 0)
                h += gh + (GALLERY_JOIN if gh > 0 else 0)
            else:
                self.gallery_host.hide()
                self.gallery_join.hide()
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
            if self.mode == "gallery":        # the gallery above keeps the keyboard
                if self.stack.currentIndex() != 1:
                    self.stack.setCurrentIndex(1)
                return
            if self.stack.currentIndex() != 1:
                self.stack.setCurrentIndex(1)
                (focus or self).setFocus(Qt.OtherFocusReason)
            elif focus is not None:
                w = QApplication.focusWidget()
                if w is None or w is self or w.isHidden():
                    focus.setFocus(Qt.OtherFocusReason)

        NAMES = {"rec": "Recording", "paused": "Paused", "starting": "Starting", "off": "Off",
                 "error": "Error", "lowstorage": "Low storage", "stopped": "Stopped"}

        def subject(self):
            """What is recorded, for "Recording …" / "Paused …"."""
            if self.target == "window":
                return self.target_name or SUBJECT["window"]
            return SUBJECT["screen"]

        def gallery_paused_view(self, view=None):
            return (self.view if view is None else view) == "paused" and self.pause_reason == "gallery"

        def view_label(self, view):
            if view == "paused" and self.pause_reason == "gallery":
                return GALLERY_PAUSED
            if view in ("rec", "paused"):
                return f"{self.NAMES[view]} {self.subject()}"
            if view == "stopped":
                return STOPPED_TEXT[self.target]
            return self.NAMES[view]

        def set_view(self, view, opts_on):
            """One bar for every state: only the dot, label, time and enablement change."""
            self.view = view
            running = self.running
            dot = RED if view in ("rec", "lowstorage") else MUTED if view == "paused" else DIM
            self.dot.setStyleSheet(f"background: {dot}; border-radius: 4px;")
            # a sentence ("Press play to ...", "Paused while the gallery is open") takes
            # the dot's and the time's room
            sentence = view == "stopped" or self.gallery_paused_view(view)
            self.dotbox.setHidden(sentence)
            label = self.view_label(view)
            room = self.stopped_w if sentence else self.name_w
            fm = self.name.fontMetrics()
            shown = label if fm.horizontalAdvance(label) <= room else fm.elidedText(label, Qt.ElideRight, room)
            if self.name.text() != shown:
                self.name.setText(shown)
            self.name.setAccessibleName(label)
            self.name.setHidden(not label)
            self.dot.setAccessibleName(self.NAMES[view])
            for c in self.controls:
                pb = c["pause"]
                if view == "off" or (view == "starting" and not running):
                    pb.set_kind("start")
                    pb.setEnabled(view == "off")
                elif view in ("paused", "lowstorage"):
                    pb.set_kind("play" if view == "paused" else "start")
                    pb.setEnabled(True)
                elif view == "stopped":
                    # window mode: play has the daemon open the window picker
                    pb.set_kind("pick" if self.target == "window" else "start")
                    pb.setEnabled(True)
                else:
                    pb.set_kind("pause")
                    pb.setEnabled(True)
                c["stop"].setEnabled(running and view not in ("off", "stopped"))
                c["shot"].setEnabled(view == "rec")   # a frame of the recording: only while recording
                for b in c.values():
                    b.show()
            for o in self.options:
                # a length longer than the replay can never be saved: greyed and skipped
                o.setEnabled(opts_on and o.seconds <= self.max_clip + 0.5)
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
            # the bar row stays live under the gallery (the time ticks on, pause works)
            if self.saving or self.done or self.mode not in ("clip", "gallery") or self.control_busy:
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
                state = st.get("state")
                self.target = "window" if st.get("target") == "window" else "screen"
                self.max_clip = float(st.get("max_seconds") or 3600)
                self.target_name = _clean_title(st.get("target_name"))
                # "no_window" (an older daemon: the picked window closed) is shown as stopped
                self.stopped = state in ("stopped", "no_window")
                kept = st.get("keep_history")
                # an older daemon kept the footage in no_window and cleared it on stop
                self.keep_history = bool(kept) if kept is not None else state == "no_window"
                self.paused = state == "paused"
                self.pause_reason = st.get("pause_reason") if self.paused else None
                buffered = float(st.get("buffered") or 0.0)
                # stopped: the footage is saveable only while the history is kept
                self.buffered = 0.0 if self.stopped and not self.keep_history else buffered
                if "storage" in st or st.get("state") == "no_storage":
                    self.warn = _status_warning(st)
                    self.soft_warn = self.warn if _status_warning_soft(st) else None
                # else: an older daemon without storage info; keep any warning a reply gave
                self.storage_hint.set_storage(st.get("storage"))
                if self.stopped:
                    view = "stopped"  # the service keeps running (hotkey), recording does not
                elif state == "no_storage":
                    view = "lowstorage"
                elif self.paused:
                    view = "paused"
                elif st.get("recording"):
                    view = "rec"
                elif st.get("state") == "error":
                    view = "error"
                else:
                    view = "starting"
                self.set_view(view, self.buffered > 0)
                self.set_live({} if self.stopped else st, view == "rec")
                shown = self.live_seconds()
                if view == "stopped" or self.gallery_paused_view(view):
                    self.set_time("", MUTED)   # the sentence takes the time's place
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
            if self.mode == "gallery":
                return                          # the gallery keeps the keyboard
            w = QApplication.focusWidget()
            if (self.online and not was_on) or w not in self.focusables():
                self.focus_default()

        def focus_default(self):
            if self.stack.currentIndex() != 0:
                self.setFocus(Qt.OtherFocusReason)
                return
            if self.online:
                want = _last_choice()
                usable = [o for o in self.options if o.isEnabled()] or self.options
                # the last length, else the longest the replay holds (30m on a 15-minute replay: 15m)
                target = next((o for o in usable if o.seconds == want), None)
                if target is None:
                    target = ([o for o in usable if o.seconds <= want] or usable)[-1]
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
            if self.live_ticking:
                self.ticker.start()   # the timer only runs while the bar is on screen
            self.after(0, self.pads_open)  # after the first paint: opening devices takes a few ms
            super().showEvent(ev)

        def hideEvent(self, ev):
            self.ticker.stop()
            self.close_gallery()      # a hidden bar never plays anything
            self.pads_close()         # a hidden bar holds no controller (and no fds)
            super().hideEvent(ev)

        def choose(self, opt):
            if self.saving or self.done or self.control_busy:
                return
            if not self.online or not opt.isEnabled():
                self.sound("error")             # nothing to save, or longer than the replay
                return
            if not self.gallery_yield():
                return
            self.sound("select")
            self.saving = True
            self.idle.stop()
            _store_choice(opt.seconds)
            shown = min(opt.seconds, self.buffered) if self.buffered else opt.seconds
            self.show_line(f"Saving last {dur_label(shown)}…")
            self.relayout()
            secs = opt.seconds
            gen = self.gen

            def work():
                try:
                    # The clip runs up to now. In full screen mode the bar itself may be
                    # in it; recording a window instead keeps it out.
                    r = ipc.request({"cmd": "save", "seconds": secs}, timeout=120)
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
            self.sound("save" if r.get("ok") else "error")    # the result, not the press
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
                    or not self.gallery_yield()):
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
            # the bar is already hidden; the sound says it worked (a one-shot bar lets it finish)
            self.sound("shot" if r.get("ok") else "error")
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
            if self.control_busy or self.saving or self.done or self.mode not in ("clip", "gallery"):
                return
            if not self.running:
                if self.view == "off":
                    self.start_recorder()
                return
            if self.view == "lowstorage" or (self.paused and _storage_short(self.last_status) is not None):
                self.sound("error")
                self.show_storage_warning()  # resuming would fail: say why instead
                return
            cmd = "resume" if self.paused or self.stopped else "pause"
            self.sound("record" if cmd == "resume" else "pause")
            # From stopped in window mode the daemon opens the window picker on resume.
            self.resume_picks = cmd == "resume" and self.stopped and self.target == "window"
            self.control_busy = True
            gen = self.gen

            def work():
                try:
                    r = ipc.request({"cmd": cmd}, timeout=30)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit(gen, cmd, r)
            threading.Thread(target=work, daemon=True).start()

        def confirm_text(self):
            """The stop question: without keep_history a stop deletes the replay, so it says so."""
            return "Stop recording?" if self.keep_history else "Stop and clear replay?"

        def ask_stop(self):
            if (not self.running or self.stopped or self.control_busy or self.saving or self.done
                    or not self.gallery_yield()):
                return
            self.sound("select")
            self.confirm.setText(self.confirm_text())
            self.mode = "confirm"
            self.stack.setCurrentIndex(3)
            self.relayout()
            self.stop_no.setFocus(Qt.OtherFocusReason)  # the safe choice is the default

        def cancel_confirm(self):
            if self.mode != "confirm" or self.control_busy:
                return
            self.sound("select")
            self.mode = "clip"
            self.back_to_clip("stop")
            self.idle.setInterval(IDLE_HIDE_MS)
            self.idle.start()                   # back to the normal auto-hide

        def confirm_stop(self):
            if self.mode != "confirm" or self.control_busy:
                return
            self.sound("stop")
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
            if cmd in ("gallery_pause", "gallery_resume"):
                self.on_gallery_pause(cmd, r)
                return
            self.control_busy = False
            picks, self.resume_picks = self.resume_picks and cmd == "resume", False
            if picks and r.get("ok"):
                # The daemon opens the window picker now; the bar hides once the request
                # is answered (a one-shot bar must not quit before it is out), so the
                # dialog can take the keyboard.
                self.close_bar()
                return
            if not r.get("ok"):
                self.sound("error")
                if r.get("code") == "no_storage":
                    self.show_storage_warning(_storage_warning(r.get("storage") or {}, r.get("error")))
                    self.status_inflight = False
                    self.refresh_async()
                else:
                    self.show_line(f"<span style='color:{RED}'>{_esc(r.get('error') or cmd + ' failed')}</span>")
                return
            if cmd == "stop":
                st = {**(self.last_status or {}), "ok": True, "state": "stopped", "recording": False}
                if r.get("buffer_cleared") is not False:   # with keep_history the footage stays
                    st.update(buffered=0, buffered_live=0)
                self.apply_status(st)
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
            self.sound("record")
            self.control_busy = True
            self.warn = None
            if self.stack.currentIndex() != 0:
                self.stack.setCurrentIndex(0)
            self.set_view("starting", False)
            self.set_time("0:00", MUTED)
            self.relayout()
            if self.mode != "gallery":
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
            if not st.get("ok"):
                self.sound("error")
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
            if self.saving or self.done or self.loading_settings or self.control_busy or not self.gallery_yield():
                return
            self.sound("select")
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
                self.sound("error")
                self.show_line(f"<span style='color:{RED}'>{_esc(data.get('error') or 'Cannot read settings')}</span>")
                return
            self.sdata = data
            self.build_rows()
            self.mode = "settings"
            self.apply_state = None
            self.stack.setCurrentIndex(2)
            self.update_foot()
            self.relayout()
            self.focus_line(1)                  # the open tab's first row
            self.idle.setInterval(IDLE_HIDE_MS)
            self.idle.start()

        def clear_rows(self):
            """Delete the settings tabs and rows (rebuilt on every open of the settings)."""
            self.rows = []
            self.tab_btns, self.tab_names, self.tabstack = [], [], None
            self.panel_rows = 0
            old = self.panel_lay.takeAt(0)
            while old is not None:
                if old.widget() is not None:
                    old.widget().hide()
                    old.widget().deleteLater()
                old = self.panel_lay.takeAt(0)

        def make_row(self, key, avail):
            """The settings row for ``key``, or None when the reply does not offer it."""
            data = self.sdata
            vals, choices = data["values"], data.get("choices") or {}
            if key not in vals:
                return None
            title = ROW_TITLES.get(key) or key.replace("_", " ").capitalize()
            value = vals[key]
            if key == "record":
                rec = [(r, RECORD_TEXT.get(r, settings.RECORD_LABELS.get(r, r)))
                       for r in choices.get("record", list(RECORD_TEXT))]
                return SettingRow(self, key, title, rec, value, avail)
            if key == "resolution":
                source, _window = self.res_source()
                allowed = quality.allowed_resolutions(source)
                return SettingRow(self, key, title, [(r, RES_LABELS.get(r, r)) for r in choices["resolution"]],
                                  value, avail, disabled=[r for r in choices["resolution"] if r not in allowed])
            if key == "fps":
                return SettingRow(self, key, title,
                                  [(f, "Auto" if f == "auto" else f"{f} fps") for f in choices.get("fps", [60])],
                                  vals.get("fps", quality.FPS), avail)
            if key == "quality":
                return SettingRow(self, key, title, [(q, q.capitalize()) for q in choices["quality"]],
                                  value, avail)
            if key == "format":
                return FormatRow(self, key, title, data, avail)
            dev = data.get("devices") or {}
            if key == "audio_source":
                sound = [("default", "Default output")] + [(d["name"], d["label"], True)
                                                           for d in dev.get("outputs") or []]
                if value not in [c[0] for c in sound] + ["off"]:
                    sound.append((value, value, True))  # an unplugged device
                sound.append(("off", "Off"))
                return SettingRow(self, key, title, sound, value, avail, cycle=len(sound) - 2 > CYCLE_OVER)
            if key == "mic_device":
                micdev = [("default", "Default mic")] + [(d["name"], d["label"], True)
                                                         for d in dev.get("inputs") or []]
                if value not in [c[0] for c in micdev]:
                    micdev.append((value, value, True))
                return SettingRow(self, key, title, micdev, value, avail, cycle=len(micdev) - 1 > CYCLE_OVER)
            if key == "controller":
                if not data.get("controller_available", True):
                    return None             # no python-evdev: the controller settings do nothing
                # Off / PS / Xbox + Down; any other saved shortcut (an older preset, a
                # hand-edited list) stays as an extra choice named by its buttons
                ctl = [(k, settings.controller_label(k)) for k in ("off", *gamepad.CHORD_OFFERED)]
                if value not in [c[0] for c in ctl]:
                    # as the controller in use labels them (Xbox names without one)
                    ctl.append((value, settings.controller_label(value, self.pad_symbols()), True))
                return SettingRow(self, key, title, ctl, value, avail)
            if key == "replay_length":
                opts = list(choices.get(key) or REPLAY_MINUTES)
                if value not in opts:
                    opts.append(value)      # a hand-edited [buffer] max_seconds, in whole minutes
                return SettingRow(self, key, title, [(m, f"{m} min") for m in opts], value, avail)
            opts = list(choices.get(key) or (("off", "on") if key in ON_OFF_KEYS else ()))
            if key == "hour_warning":
                if value not in opts:
                    opts.append(value)      # set by hand to another whole number of minutes
                return SettingRow(self, key, title, [(m, f"{m} min") for m in opts], value, avail)
            if not opts:
                return None
            if value not in opts:
                opts.append(value)
            return SettingRow(self, key, title,
                              [(c, c.capitalize() if isinstance(c, str) else str(c)) for c in opts], value, avail)

        def build_rows(self):
            self.clear_rows()
            data = self.sdata
            vals = data["values"]
            for k in ON_OFF_KEYS:         # a daemon may send booleans
                if isinstance(vals.get(k), bool):
                    vals[k] = "on" if vals[k] else "off"
            avail = self.bar_w - 2 - 16 - GLYPH_W - GLYPH_GAP - LABEL_W - SEG_SPACING - 12

            tabs, seen = [], set()
            for name, keys in _tabs(data, getattr(settings, "TABS", None) or DEFAULT_TABS):
                rows = []
                for k in keys:
                    r = None if k in seen else self.make_row(k, avail)
                    if r is not None:
                        seen.add(k)
                        rows.append(r)
                if name == "Misc" and rows:
                    rows.append(ReportRow(self, avail))
                if rows:
                    tabs.append((name, rows))

            # the tab row, with the note on the right
            header = QWidget()
            header.setFixedHeight(TABS_H)
            hl = QHBoxLayout(header)
            hl.setContentsMargins(12, 0, 18, 0)   # the first pill lines up with the row icons
            hl.setSpacing(0)
            for i, (name, _rows) in enumerate(tabs):
                b = TabButton(name)
                b.clicked.connect(lambda _=False, i=i: self.click_tab(i))
                hl.addWidget(b)
                self.tab_btns.append(b)
            hl.addStretch(1)
            self.note = Note(Qt.AlignRight, lines=1)
            tabs_w = sum(b.width() for b in self.tab_btns)
            self.note.fix_width(self.bar_w - 2 - 12 - 18 - tabs_w - NOTE_GAP)
            self.note.set(*self.header_note(), animate=False)
            hl.addWidget(self.note)
            self.panel_lay.addWidget(header)

            # one page of rows per tab
            self.tabstack = QStackedWidget()
            for i, (_name, rows) in enumerate(tabs):
                page = QWidget()
                pl = QVBoxLayout(page)
                pl.setContentsMargins(0, 0, 0, 0)
                pl.setSpacing(0)
                for r in rows:
                    r.tab = i
                    pl.addWidget(r)
                pl.addStretch(1)
                self.tabstack.addWidget(page)
            self.panel_lay.addWidget(self.tabstack)
            self.tab_names = [n for n, _r in tabs]
            self.rows = [r for _n, rows in tabs for r in rows]
            self.panel_rows = max((len(rows) for _n, rows in tabs), default=0)
            mic, micdev = self.row("mic"), self.row("mic_device")
            if mic is not None and micdev is not None:
                micdev.setHidden(mic.value != "on")
            self.show_tab(self.tab_names.index(self.last_tab) if self.last_tab in self.tab_names else 0)
            self.update_fit()
            self.track_mouse(self.panel)
            self.update_res_note()
            self.update_fps_note()

        # ---- the resolution cap
        def res_source(self):
            """(size, is_window) of the picture that caps Resolution: the daemon's source_size
            (the screen, or the picked window), else the largest screen; (None, False) unknown."""
            data = self.sdata or {}
            source = quality.source_size(data.get("source_size"))
            if source is not None:
                return source, (data.get("values") or {}).get("record") == "window"
            return largest_screen(), False

        def res_note(self, value):
            """"Your screen is 1080p", or "Recording at 1080p (your screen)" when ``value`` is
            above it. Empty when nothing is capped (no clutter on a 4K screen)."""
            row = self.row("resolution")
            source, window = self.res_source()
            if source is None or row is None or not row.disabled:
                return ""
            if window:
                size = f"{source[0]}\u00d7{source[1]}"
            else:
                size = RES_LABELS.get(quality.height_label(source), quality.height_label(source))
            if quality.effective_resolution(value, source) != str(value).lower():
                return f"Recording at {size} ({'window size' if window else 'your screen'})"
            return f"Window is {size}" if window else f"Your screen is {size}"

        def update_res_note(self):
            row = self.row("resolution")
            if row is not None:
                row.set_note(self.res_note(row.value))

        def screen_refresh(self):
            """The recorded screen's refresh rate (Hz): the daemon's (refresh_hz), else the
            refresh of the screen the bar is on; None when neither is known."""
            hz = quality.refresh_hz((self.sdata or {}).get("refresh_hz"))
            return hz if hz is not None else local_refresh(self)

        def fps_effective(self, value):
            """What frame rate setting ``value`` records at: Auto by the screen's refresh."""
            try:
                return quality.fps({"fps": value}, self.screen_refresh())
            except ValueError:
                return value

        def fps_note(self, value):
            """The Frame rate row's note: what Auto matches, or (120 picked by hand on a
            screen below 100 Hz) that 120 won't help. Empty otherwise."""
            hz = self.screen_refresh()
            label = quality.hz_label(hz)
            if value == "auto":
                if label is None:
                    return FPS_AUTO_UNKNOWN_NOTE
                fps = self.fps_effective(value)
                if str(fps) == label:
                    return FPS_AUTO_NOTE.format(hz=label)
                return FPS_AUTO_OTHER_NOTE.format(fps=fps, hz=label)
            if value == 120 and hz is not None and hz < quality.AUTO_HIGH_HZ:
                return FPS_NOTE.format(hz=label)
            return ""

        def update_fps_note(self):
            """Auto says which screen it matches; 120 on a slower screen says it won't help."""
            row = self.row("fps")
            if row is not None:
                row.set_note(self.fps_note(row.value))

        # ---- tabs
        def show_tab(self, i):
            self.tab = i
            if not self.tab_btns:
                return
            self.last_tab = self.tab_names[i]
            for j, b in enumerate(self.tab_btns):
                b.set_sel(j == i)
            self.tabstack.setCurrentIndex(i)

        def switch_tab(self, i, focus):
            """Open tab ``i``. ``focus``: "tab" (stay on the tab row), "row" (its first row),
            "keep" (the footer keeps focus), None (a click: focus only if it was lost)."""
            if not self.tab_btns or self.mode != "settings":
                return
            i = max(0, min(len(self.tab_btns) - 1, i))
            if i != self.tab:
                self.show_tab(i)
            if focus == "tab":
                self.tab_btns[i].setFocus(Qt.TabFocusReason)
            elif focus == "row":
                self.focus_line(1)
            else:
                w = QApplication.focusWidget()
                if w is None or w is self or not w.isVisible():
                    self.tab_btns[i].setFocus(Qt.OtherFocusReason)

        def step_tab(self, d):
            """Bumpers / Page Up-Down: the previous / next tab, focus staying on its line."""
            if self.apply_state in ("busy", "done"):
                return
            pos, _i = self.settings_pos()
            self.switch_tab(self.tab + d, {"tabs": "tab", "foot": "keep"}.get(pos, "row"))

        def row(self, key):
            return next((r for r in self.rows if r.key == key), None)

        def visible_rows(self):
            """The open tab's rows that are showing (Mic device only with the mic on)."""
            return [r for r in self.rows if r.tab == self.tab and not r.isHidden()]

        def pending(self):
            """Every value as the rows (on all tabs) now say, over the reply's values."""
            return {**self.sdata["values"],
                    **{r.key: r.value for r in self.rows if not getattr(r, "action", False)}}

        def changes(self):
            vals = self.sdata["values"]
            out = {k: v for k, v in self.pending().items() if vals.get(k) != v}
            for r in self.rows:
                if getattr(r, "forced", lambda: False)():
                    out[r.key] = r.value   # a retry of the saved value (a format that crashed)
            return out

        def live_keys(self):
            """Settings the daemon applies without restarting the recording."""
            return set(getattr(settings, "LIVE_KEYS", None) or LIVE_KEYS) | set(settings.CONTROLLER_KEYS)

        def header_note(self):
            """(text, kind) for the line next to the tabs: what Apply will do with the
            changes made so far. Plain (no glyph: a status line, not a note); dropping
            footage is a warning."""
            data = self.sdata or {}
            if not data.get("online"):
                return "Momento is off — changes apply when it starts", "plain"
            ch = self.changes() if self.rows else {}
            was, now = data.get("values", {}).get("replay_length"), ch.get("replay_length")
            if isinstance(was, int) and isinstance(now, int) and now < was:
                return f"Keeps the newest {now} min · older footage is dropped", "warn"
            if ch and set(ch) <= self.live_keys():
                return "Applies right away · your replay is kept", "plain"
            return "Applying restarts recording · your replay is kept", "plain"

        def note_text(self):
            return self.header_note()[0]

        def replay_seconds(self, v=None):
            """The replay length the rows now say, in seconds (the saved max_seconds, exact,
            while the row is unchanged: a hand-edited length is not rounded)."""
            v = self.pending() if v is None else v
            saved = int(self.sdata.get("max_seconds") or 3600)
            new = v.get("replay_length")
            if not isinstance(new, int) or new == self.sdata["values"].get("replay_length"):
                return saved
            return new * 60

        def estimate(self):
            v = self.pending()
            cap = {"resolution": v["resolution"], "quality": v["quality"], "fps": v.get("fps", quality.FPS),
                   "bitrate_kbps": self.sdata["values"].get("bitrate", 0)}
            secs = self.replay_seconds(v)
            # what is really recorded: a resolution above the picture costs the picture's size,
            # and Auto the frame rate it records at on this screen
            hz = self.screen_refresh()
            gb = quality.buffer_gb(quality.bitrate_kbps(cap, self.res_source()[0], hz), secs)
            return f"{self.fps_effective(cap['fps'])} fps · ~{gb:.1f} GB for {secs // 60} min"

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
            required = sto.get("required") or {}
            fps = v.get("fps", quality.FPS)
            if fps == "auto" and f"{v['resolution']}/{v['quality']}/{self.fps_effective(fps)}" in required:
                fps = self.fps_effective(fps)   # Auto costs what it records at (as the estimate says)
            need = required.get(f"{v['resolution']}/{v['quality']}/{fps}")
            if need is None:
                return None
            # the reply counts the saved replay length; the buffer part scales with the length
            saved = int(self.sdata.get("max_seconds") or 3600)
            secs = self.replay_seconds(v)
            if secs != saved and saved > 0:
                reserve = float(sto.get("reserve") or 0)
                need = reserve + (float(need) - reserve) * secs / saved
            return float(need)

        def fits(self, v):
            need, free = self.storage_need(v), self.storage_free()
            return need is None or free is None or need <= free

        def current_need(self):
            sto = (self.sdata or {}).get("storage") or {}
            cur = sto.get("current")
            if cur and cur in (sto.get("required") or {}) and not cur.endswith("/auto"):
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
            for key in ("resolution", "quality", "fps", "replay_length"):
                row = self.row(key)
                if row is None or row.cycle:
                    continue
                for val, b in zip(row.values, row.buttons):
                    b.set_nofit(b.isEnabled() and not self.fits({**v, key: val}))
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
                self.foot.set(f"Needs {_gb(self.storage_need(v))} GB · {_gb(self.storage_free())} GB free", "warn")
            else:
                self.foot.set(self.estimate(), "plain")

        def on_row_changed(self, row):
            if row.key == "resolution":
                self.update_res_note()
            elif row.key == "fps":
                self.update_fps_note()
            self.note.set(*self.header_note())
            if row.key == "mic" and self.row("mic_device") is not None:
                # the panel keeps its height (sized for the tallest tab): nothing moves
                self.row("mic_device").setHidden(row.value != "on")
            if self.apply_state == "error":
                self.apply_state = None
            self.update_fit()
            self.update_foot()

        def close_settings(self, focus_key="gear"):
            """Back (Esc, B, the Back button; ``focus_key`` None: after Apply, no sound)."""
            if self.mode != "settings" or self.apply_state == "busy":
                return
            if focus_key is not None:
                self.sound("select")
            self.apply_state = None
            self.idle.setInterval(IDLE_HIDE_MS)
            self.idle.start()
            self.back_to_clip(focus_key)
            if focus_key is None:
                self.focus_default()  # after Apply: back on a clip length, not the gear
            self.refresh_async()

        def apply_settings(self):
            if self.mode != "settings" or self.apply_state in ("busy", "done"):
                return
            if not self.apply_btn.isEnabled():
                self.sound("error")
                return  # the chosen combination needs more space than is free
            changes = self.changes()
            if not changes:
                self.close_settings()
                return
            self.sound("select")
            self.apply_state = "busy"
            self.applied = dict(changes)
            self.idle.stop()
            self.foot.set("Applying…", "status")
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
                        r = ipc.request({"cmd": "configure", "changes": changes, "origin": "bar"},
                                        timeout=60)
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
                self.sound("error")
                self.apply_state = "error"
                self.foot.set(str(r.get("error") or "Could not save"), "error")
                self.idle.start()
                return
            self.apply_state = "done"
            if "sounds" in (self.applied or {}):
                self.sounds_on = self.applied["sounds"] == "on"   # the bar follows its own Apply
            if r.get("warning") or r.get("state") == "no_storage":
                # saved, but even the new settings do not fit yet
                msg = str(r.get("warning") or "Not enough free space")
                self.foot.set(f"Saved · {msg}", "error")
                self.last_status = {**(self.last_status or {}), "ok": True, "state": "no_storage",
                                    "recording": False, "error": r.get("warning")}
                self.after(APPLY_CLOSE_MS, self.after_apply)
                return
            # Controller settings (and Replay length, Keep history, Hour warning, Instant
            # bar) apply at once, without restarting the recording.
            applied = set(self.applied or ())
            live_keys = self.live_keys()
            pads_only = bool(applied) and applied <= set(settings.CONTROLLER_KEYS)
            live = (bool(applied) and applied <= live_keys) or r.get("restarted") is False
            if not r.get("online"):
                tail = "takes effect when Momento starts"
            elif pads_only:
                tail = "controller updated"
            elif r.get("paused"):
                tail = "applies when you resume"
            elif live:
                tail = None
            else:
                tail = "recording restarted"
            self.foot.set(tail or "", "plain", lead="Saved")
            if r.get("online") and not r.get("paused") and not live:
                # the recorder restarts; footage already buffered stays saveable
                self.last_status = {**(self.last_status or {}), "ok": True, "state": "starting",
                                    "recording": False}
                if (r.get("changed") or {}).get("record") == "window":
                    # the desktop's window picker opens now: get out of its way
                    self.foot.set("pick a window", "plain", lead="Saved")
                    self.after(0, self.after_apply)
                    return
            self.after(APPLY_CLOSE_MS, self.after_apply)

        def after_apply(self):
            """After Apply: back to the clip view (the state a new open starts in), then hide."""
            if self.mode == "settings" and self.apply_state == "done":
                self.apply_state = None
                self.close_settings(focus_key=None)
                self.close_bar()

        def settings_pos(self):
            """Where focus is in settings: ("tabs", i), ("row", i) in visible_rows(), ("foot", 0)."""
            w = QApplication.focusWidget()
            if w in self.tab_btns:
                return "tabs", self.tab_btns.index(w)
            for i, r in enumerate(self.visible_rows()):
                if w is not None and r.isAncestorOf(w):
                    return "row", i
            if w in (self.apply_btn, self.back_btn):
                return "foot", 0
            return "row", 0

        def focus_line(self, line):
            """Settings are lines top to bottom: 0 the tabs, 1..n the open tab's rows, n+1 the footer."""
            rows = self.visible_rows()
            line = max(0, min(len(rows) + 1, line))
            if line == 0 and self.tab_btns:
                self.tab_btns[self.tab].setFocus(Qt.TabFocusReason)
            elif 0 < line <= len(rows):
                rows[line - 1].focus()
            elif line == 0 and rows:
                rows[0].focus()
            else:
                (self.apply_btn if self.apply_btn.isEnabled() else self.back_btn).setFocus(Qt.TabFocusReason)

        def settings_key(self, k):
            if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
                self.close_settings()
                return True
            if self.apply_state in ("busy", "done"):
                return True
            rows = self.visible_rows()
            pos, i = self.settings_pos()
            if pos == "row" and not rows:
                pos = "tabs"
            line = {"tabs": 0, "row": 1 + i, "foot": 1 + len(rows)}[pos]
            if k in (Qt.Key_PageUp, Qt.Key_PageDown):
                self.step_tab(-1 if k == Qt.Key_PageUp else 1)
            elif k in (Qt.Key_Up, Qt.Key_Backtab, Qt.Key_Down, Qt.Key_Tab):
                self.focus_line(line + (-1 if k in (Qt.Key_Up, Qt.Key_Backtab) else 1))
            elif k in (Qt.Key_Left, Qt.Key_Right):
                d = -1 if k == Qt.Key_Left else 1
                if pos == "tabs":
                    self.switch_tab(self.tab + d, "tab")
                elif pos == "row":
                    rows[i].step(d)
                else:
                    tgt = self.apply_btn if d < 0 and self.apply_btn.isEnabled() else self.back_btn
                    tgt.setFocus(Qt.TabFocusReason)
            elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select):
                if pos == "tabs":
                    self.focus_line(1)          # into the tab
                elif self.back_btn.hasFocus():
                    self.close_settings()
                elif pos == "row" and getattr(rows[i], "action", False):
                    rows[i].activate()          # Make a report / Open logs
                else:
                    self.apply_settings()       # one Apply for the changes on every tab
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
                          on_action=self.on_pad_action, on_chord=self.on_pad_chord,
                          on_button=self.on_pad_button, on_devices=self.on_pads_changed,
                          on_active=self.on_pads_changed)
            try:
                ok = hub.start()
            except Exception:  # noqa: BLE001
                log.exception("controller support failed to start")
                ok = False
            if not ok:
                hub.close()
                return
            self.pads = hub
            if self.pad_used in getattr(hub, "pads", {}):
                hub.last_input_key = self.pad_used    # still there: its buttons in the hints
            self.pads_handle = hub.attach_qt(self)
            if ctl["exclusive"]:
                hub.grab()
            self.on_pads_changed()

        def pads_close(self):
            hub, self.pads = self.pads, None
            handle, self.pads_handle = self.pads_handle, None
            if getattr(hub, "last_input_key", None) is not None:
                self.pad_used = hub.last_input_key    # the next open starts with its symbols
            try:
                if handle is not None:
                    handle.detach()
            finally:
                if hub is not None:
                    hub.close()       # ungrabs first

        def on_pad_action(self, action, repeat=False):
            if not self.isVisible():
                return
            self.with_sounds(self._pad_action, action)

        def _pad_action(self, action):
            self.touch_idle()         # like a key press: restart the auto-hide
            self.set_focus_visible(True)
            if self.mode == "gallery" and self.gallery is not None:
                self.gallery.pad(action)
                return
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

        def pad_connected(self):
            """A controller drives the bar now (its hub has one open): the gallery's hints
            show its buttons; without one (or with controllers off) they show the keys."""
            devices = getattr(self.pads, "devices", None)
            try:
                return bool(devices()) if devices is not None else False
            except Exception:  # noqa: BLE001 - only decides which hints show
                return False

        def pad_symbols(self):
            """Whose button names the hints show: "playstation" (✕ ○ □ △, L1 / R1),
            "nintendo" or "xbox", from the controller in use (``Gamepads.symbols``)."""
            symbols = getattr(self.pads, "symbols", None)
            try:
                return symbols() if symbols is not None else "xbox"
            except Exception:  # noqa: BLE001 - only decides which hints show
                return "xbox"

        def on_pads_changed(self):
            """A controller came or went, or another one is in use (or the hub opened):
            the hints follow."""
            if self.gallery is not None and self.mode == "gallery":
                self.gallery.sync_hints()

        def on_pad_button(self, name, pressed):
            """Any controller button (a trigger in the clip view too) counts as use."""
            if self.isVisible():
                self.touch_idle()

        def pad_section(self, d):
            """Bumpers: jump between groups (clip lengths / buttons) or settings tabs."""
            if self.mode == "settings":
                self.step_tab(d)                # bumpers switch settings tabs
            elif self.mode == "confirm":
                self.confirm_key(Qt.Key_Left)
            elif not (self.saving or self.done or self.control_busy):
                # groups, left to right: the gallery button, the clip lengths, the buttons
                items = self.focusables()
                gal = [w for w in items if w is self.gallery_btn]
                opts = [w for w in items if w in self.options]
                rest = [w for w in items if w not in self.options and w is not self.gallery_btn]
                cur = QApplication.focusWidget()
                if d < 0:
                    if opts and cur not in opts and cur not in gal:
                        self.focus_default()
                    elif opts and cur in opts and cur is not opts[0]:
                        opts[0].setFocus(Qt.TabFocusReason)
                    elif gal:
                        gal[0].setFocus(Qt.TabFocusReason)
                elif cur in gal and opts:
                    self.focus_default()
                elif rest:
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
                self.sound("close")
                self.close_bar()

        # ---------------- gallery
        def open_gallery(self):
            """The gallery button / G: list the clips folder (in a worker), then open the gallery
            above the bar, or say there is nothing yet."""
            if self.mode == "gallery" and self.gallery is not None:
                self.gallery.back()           # the button toggles, like G
                return
            if self.mode != "clip" or self.saving or self.done or self.control_busy:
                return
            if self.gallery is None:
                import types

                from . import gallery as gallery_mod   # QtMultimedia only from here on

                kit = types.SimpleNamespace(
                    Pill=Pill, TextButton=TextButton, IconButton=IconButton, TabButton=TabButton,
                    ui_font=ui_font, draw_line_glyph=_draw_line_glyph, divider=divider,
                    layer_full=_apply_layer_shell_full, set_keyboard=_set_keyboard_interactivity)
                self.gallery = gallery_mod.Gallery(self, kit)
                self.track_mouse(self.gallery_host)
            self.gallery.open()

        def gallery_empty(self):
            """Nothing saved yet: one line above the bar, for a moment."""
            self.sound("select")
            self.gallery_hint = GALLERY_EMPTY
            self.relayout()
            self.gallery_btn.setFocus(Qt.OtherFocusReason)

            def clear():
                self.gallery_hint = None
                self.relayout()
            self.after(GALLERY_HINT_MS, clear)

        def enter_gallery(self):
            """Called by the gallery once it has something to show."""
            self.sound("gallery_open")
            self.mode = "gallery"
            self.gallery_hint = None
            self.recycle = True       # its video libraries stay loaded: start over once hidden
            self.gallery_btn.set_on(True)
            self.relayout()
            self.idle.setInterval(GALLERY_IDLE_MS)
            self.touch_idle()
            self.pad_renew.start()
            self.gallery_hold()

        def leave_gallery(self):
            """Back from the gallery to the clip view, on the gallery button."""
            self.sound("gallery_close")
            self.pad_renew.stop()
            self.mode = "clip"
            self.gallery_btn.set_on(False)
            self.gallery_release()
            self.idle.setInterval(IDLE_HIDE_MS)
            self.back_to_clip()
            if self.stack.currentIndex() == 0:
                self.gallery_btn.setFocus(Qt.OtherFocusReason)
            if self.isVisible():
                self.idle.start()

        def close_gallery(self):
            """Tear the gallery down (player, audio, full screen) without moving focus."""
            self.pad_renew.stop()
            if self.gallery is not None:
                self.gallery.close()
            self.gallery_btn.set_on(False)
            self.gallery_release()
            if self.mode == "gallery":
                self.mode = "clip"
                self.idle.setInterval(IDLE_HIDE_MS)

        # Full screen mode records the whole screen, gallery included: recording pauses
        # while it is open and picks up again when it closes. The daemon keeps the reason
        # ("gallery"), resumes only a pause the gallery still holds (a pause or play by
        # the user takes it over), and resumes by itself if this process goes away.
        def gallery_hold(self):
            if self.gallery_pause is not None or self.target != "screen" or self.view != "rec":
                return
            self.gallery_pause = "asked"
            self._gallery_send({"cmd": "pause", "reason": "gallery", "pid": os.getpid()})

        def gallery_release(self):
            if self.gallery_pause is None:
                return
            self.gallery_pause = None
            self._gallery_send({"cmd": "resume", "reason": "gallery"})   # a no-op unless still held
            if self.pause_reason == "gallery" and self.last_status and self.last_status.get("ok"):
                # show it right away; the next poll confirms
                self.apply_status({**self.last_status, "state": "starting", "recording": False,
                                   "pause_reason": None})

        def _gallery_send(self, msg):
            if self._gallery_jobs is None:
                from concurrent.futures import ThreadPoolExecutor

                # one worker: a resume never overtakes its pause; exit waits for the last one
                self._gallery_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gallery-pause")
            gen = self.gen

            def work():
                try:
                    r = ipc.request(msg, timeout=10)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e) or e.__class__.__name__}
                self.bridge.control.emit(gen, f"gallery_{msg['cmd']}", r)
            self._gallery_jobs.submit(work)

        def on_gallery_pause(self, what, r):
            """The answer to the gallery's pause ("gallery_pause") or resume (not a protocol cmd)."""
            if what == "gallery_pause" and self.gallery_pause == "asked":
                held = bool(r.get("ok")) and r.get("pause_reason") == "gallery"
                if held:
                    self.gallery_pause = "held"
                elif r.get("ok"):
                    self.gallery_pause = None   # it didn't pause (window mode, not recording)
                # no answer: stays "asked", so closing still sends the (harmless) resume
                if held and self.last_status and self.last_status.get("ok"):
                    self.apply_status({**self.last_status, "state": "paused", "recording": False,
                                       "pause_reason": "gallery"})
            self.status_inflight = False
            self.refresh_async()

        def gallery_yield(self):
            """Before a bar action that takes the bar row or hides the bar (a save, the stop
            question, settings, a screenshot): the gallery folds away first. True when the
            bar is in the clip view (now)."""
            g = self.gallery
            if self.mode == "gallery" and g is not None and g.full is None:
                g.back()
            return self.mode == "clip"

        def renew_pads(self):
            if self.pads is not None and self.mode == "gallery":
                self.pads.renew()

        def touch_idle(self):
            """Input: restart the auto-hide (3 s; 10 s in the gallery). Never while a gallery
            clip plays, and not while a save / apply / stop runs (its result closes the bar)."""
            if self.saving or self.done or not self.isVisible():
                return
            if self.reporting:
                self.idle.stop()                # the report's result shows before the bar hides
                return
            if self.mode == "gallery" and self.gallery is not None and self.gallery.playing():
                self.idle.stop()
                return
            self.idle.setInterval(GALLERY_IDLE_MS if self.mode == "gallery" else IDLE_HIDE_MS)
            self.idle.start()

        def track_mouse(self, root):
            """Pointer moves over ``root`` and everything in it reach eventFilter (as use)."""
            for w in [root] + root.findChildren(QWidget):
                w.setMouseTracking(True)

        def leave_exempt(self):
            """Watching a clip with the pointer parked elsewhere is normal; full screen too."""
            g = self.gallery
            return self.mode == "gallery" and g is not None and (g.playing() or g.full is not None
                                                                 or g.asking())

        def on_leave(self):
            if not self.isVisible() or self.leave_exempt():
                return
            if self.saving or self.done or self.control_busy or self.apply_state == "busy":
                return                          # its result closes the bar (RESULT_CLOSE_MS ...)
            self.request_close()

        # ---------------- input
        def on_idle(self):
            if self.apply_state == "busy" or self.control_busy:
                self.idle.start()
                return
            g = self.gallery
            if self.mode == "gallery" and g is not None and g.asking():
                g.cancel_delete(sound=False)    # the delete question times out to Cancel, the bar stays
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
                if self.sounds is not None and self.sounds.busy():
                    self.hide()               # gone now; the sound finishes before the exit
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
            self.close_gallery()
            self._sound_batch = None
            self.gallery_hint = None
            self.reporting = False              # a report still being written only opens its folder
            self.idle.stop()
            self.leave.stop()
            self.poll.stop()
            self.ticker.stop()
            self.live_ticking = False
            self.saving = self.done = False
            self.control_busy = self.stopping = self.loading_settings = False
            self.resume_picks = False
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
            self.idle.setInterval(IDLE_HIDE_MS)
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
            self.sounds_start()
            self.sound("open")

        def dismiss(self):
            """Hide the resident bar. It lets go of the keyboard and stops polling."""
            self.gen += 1
            self.close_gallery()                # before the hide: nothing plays once it is gone
            self.idle.stop()
            self.leave.stop()
            self.poll.stop()
            self.ticker.stop()
            if self.isVisible():
                self.hide()
            if self.layered:
                _set_keyboard_interactivity(self, False)
            self.clear_rows()                   # rebuilt on the next open of the settings
            if self.resident and self.recycle:
                self.after(0, self.recycle_now)

        def recycle_now(self):
            """After the gallery: exit (BAR_RECYCLE_EXIT) so the daemon starts a fresh bar
            at once, giving back the ~120 MB QtMultimedia and the decoders keep. Only a
            hidden resident bar does this; ``after`` drops it if the bar was shown again."""
            if not self.resident or not self.recycle or self.isVisible():
                return
            log.info("recycling the clip bar after the gallery")
            self.sounds_close()                 # the close sound finishes, then it's freed
            self.request_exit(BAR_RECYCLE_EXIT)

        def request_exit(self, code):
            QApplication.instance().exit(code)

        def focusables(self):
            page = self.stack.currentIndex()
            if page not in (0, 1):
                return []
            c = self.controls[page]
            items = ([self.gallery_btn] + self.options if page == 0 else []) + [c[k] for k in self.CONTROL_ORDER]
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
            if self.mode == "gallery" and self.gallery is not None:
                return self.gallery.key(k)
            if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
                if not self.saving:               # (a save in flight keeps the bar up)
                    self.sound("close")
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
                elif idx < len(self.options):
                    self.sound("error")           # a greyed length
            elif k == Qt.Key_S:
                self.open_settings()
            elif k == Qt.Key_G:
                self.open_gallery()
            elif k == Qt.Key_P:
                self.toggle_pause()
            else:
                return False
            return True

        def pills(self):
            own = [b for b in self.findChildren(QPushButton) if isinstance(b, Pill)]
            return own + (self.gallery.window_pills() if self.gallery is not None else [])

        def set_focus_visible(self, on):
            """Keyboard focus is drawn only while the keyboard (or a controller) is in use;
            the mouse hides it until the next key press, like :focus-visible on the web."""
            if self.focus_visible == on:
                return
            self.focus_visible = on
            for b in self.pills():
                b.sync()
            if self.gallery is not None and self.mode == "gallery":
                self.gallery.on_focus_visible()

        def settle(self):
            """Jump every running pill transition and note crossfade to its end (screenshots, tests)."""
            for b in self.pills():
                b.settle()
            for n in self.findChildren(Note):
                n.settle()

        def eventFilter(self, obj, ev):
            t = ev.type()
            if t in (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.TouchBegin, QEvent.Wheel,
                     QEvent.MouseMove, QEvent.HoverMove) and self.isVisible():
                self.touch_idle()      # restart the idle auto-close
            if self.isVisible() and isinstance(obj, QWidget):
                if t == QEvent.Leave and obj is self:
                    if not self.leave_exempt():
                        self.leave.start()      # the pointer left the bar
                elif (t in (QEvent.Enter, QEvent.MouseMove, QEvent.HoverMove, QEvent.MouseButtonPress)
                      and obj.window() is self):
                    self.leave.stop()           # ...and came back
            if t in (QEvent.MouseButtonPress, QEvent.TouchBegin) and self.isVisible():
                self.set_focus_visible(False)
                if (t == QEvent.MouseButtonPress and isinstance(obj, Pill) and not obj.isEnabled()
                        and obj.isVisible()):
                    self.sound("error")         # a greyed pill (Qt drops the click itself)
            if t == QEvent.KeyPress and self.isVisible():
                self.set_focus_visible(True)
                return self.with_sounds(self.handle_key, ev)
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
    return app, use_layer_shell


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
    bar.sounds_start()
    bar.sound("open")

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
        bar.sounds_close()   # a closing sound finishes (the bar is hidden by then)
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
                bar.sound("close")      # the hotkey or the controller shortcut again
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
        bar.sounds_close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
