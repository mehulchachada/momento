"""The replay picker: a slim bar at the bottom of the screen.

Launched by the hotkey (or ``momento overlay``). Running it while another
bar is open closes the open one instead, so the same key toggles it.

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
import os
import signal
import sys
import threading
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

# Palette. The record dot is the only accent colour.
BG = (17, 17, 17, 240)   # #111111 at ~94 %
BORDER = "#2A2A2A"
TEXT = "#EDEDED"
MUTED = "#8A8A8A"
DIM = "#555555"
RED = "#FF4D2E"


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

def _build(argv=None):  # noqa: C901 - one cohesive UI builder
    from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QPainter, QPen
    from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
                                   QSizePolicy, QStackedWidget, QWidget)

    from . import ipc

    class Bridge(QObject):
        status = Signal(object)
        saved = Signal(object)

    def fetch_status(timeout=2.0):
        try:
            return ipc.request({"cmd": "status"}, timeout=timeout)
        except Exception as e:  # DaemonNotRunning or anything else
            return {"ok": False, "not_running": isinstance(e, getattr(ipc, "DaemonNotRunning", ())),
                    "error": str(e)}

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

    def hint():
        h = QLabel("Esc")
        h.setObjectName("muted")
        h.setContentsMargins(4, 0, 8, 0)
        return h

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
                self.style().unpolish(self)
                self.style().polish(self)

    class Bar(QWidget):
        def __init__(self):
            super().__init__()
            self.saving = False
            self.done = False
            self.online = None
            self.buffered = 0.0
            self.status_inflight = False
            self.bridge = Bridge()
            self.bridge.status.connect(self.on_status)
            self.bridge.saved.connect(self.on_saved)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAutoFillBackground(False)
            self.setWindowTitle("Replay")
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFont(ui_font())
            self.setStyleSheet(f"""
                QWidget {{ color: {TEXT}; background: transparent; }}
                QLabel#muted {{ color: {MUTED}; }}
                QPushButton {{ color: {TEXT}; border: none; border-radius: 6px; outline: none;
                               margin: 7px 0; padding: 0 10px; }}
                QPushButton[long="true"] {{ color: {DIM}; }}
                QPushButton:hover, QPushButton:focus {{ background: {TEXT}; color: #111111; }}
            """)

            outer = QHBoxLayout(self)
            outer.setContentsMargins(1, 1, 1, 1)
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
            self.dot.setStyleSheet(f"background: {RED}; border-radius: 4px;")
            row.addWidget(self.dot)
            row.addSpacing(10)
            name = QLabel("Replay")
            name.setObjectName("muted")
            row.addWidget(name)
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
            row.addSpacing(6)
            row.addWidget(hint())
            self.stack.addWidget(picker)

            # page 1: a single line (saving / saved / error / off)
            line = QWidget()
            lrow = QHBoxLayout(line)
            lrow.setContentsMargins(18, 0, 0, 0)
            lrow.setSpacing(0)
            self.line = QLabel("")
            self.line.setTextFormat(Qt.RichText)
            self.line.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lrow.addWidget(self.line, 1)
            lrow.addSpacing(8)
            lrow.addWidget(divider())
            lrow.addSpacing(6)
            lrow.addWidget(hint())
            self.stack.addWidget(line)

            # Width is fixed to the picker so the bar never jumps between states.
            self.setFixedSize(picker.sizeHint().width() + 2, BAR_HEIGHT + 2)

            self.idle = QTimer(self)
            self.idle.setSingleShot(True)
            self.idle.setInterval(IDLE_CLOSE_MS)
            self.idle.timeout.connect(self.request_close)
            self.poll = QTimer(self)
            self.poll.setInterval(1000)
            self.poll.timeout.connect(self.refresh_async)

        def paintEvent(self, ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(BORDER), 1))
            p.setBrush(QColor(*BG))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
            p.end()

        def show_line(self, markup):
            self.line.setText(markup)
            self.stack.setCurrentIndex(1)
            self.setFocus(Qt.OtherFocusReason)

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
            if self.saving or self.done:
                return
            if not st.get("ok"):
                self.online = False
                self.buffered = 0.0
                if st.get("not_running", True):
                    self.show_line(f"<span style='color:{MUTED}'>Replay is off — start it with:</span>"
                                   "&nbsp; momento daemon")
                else:
                    self.show_line(f"<span style='color:{RED}'>"
                                   f"{_esc(st.get('error') or 'Cannot reach the recorder')}</span>")
                return
            self.buffered = float(st.get("buffered") or 0.0)
            self.time.setText(_mmss(self.buffered))
            self.dot.setVisible(bool(st.get("recording")))
            if self.buffered <= 0:
                self.online = False
                msg = ("Recording — nothing buffered yet" if st.get("recording")
                       else f"Replay is {_esc(st.get('state') or 'idle')} — nothing buffered")
                self.show_line(f"<span style='color:{MUTED}'>{msg}</span>")
                return
            first = self.online is not True
            self.online = True
            self.stack.setCurrentIndex(0)
            for o in self.options:
                o.set_long(o.seconds > self.buffered + 0.5)
            if first:
                self.focus_default()

        def focus_default(self):
            if self.stack.currentIndex() != 0:
                self.setFocus(Qt.OtherFocusReason)
                return
            want = _last_choice()
            target = next((o for o in self.options if o.seconds == want), self.options[0])
            target.setFocus(Qt.OtherFocusReason)

        # ---------------- save
        def choose(self, opt):
            if self.saving or self.done or not self.online:
                return
            self.saving = True
            self.idle.stop()
            _store_choice(opt.seconds)
            shown = min(opt.seconds, self.buffered) if self.buffered else opt.seconds
            self.show_line(f"Saving last {dur_label(shown)}…")
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

        # ---------------- input
        def request_close(self):
            if self.saving:
                return  # let the save finish so the result is visible
            QApplication.instance().quit()

        def move_focus(self, step):
            if self.stack.currentIndex() != 0:
                return
            cur = next((o for o in self.options if o.hasFocus()), None)
            if cur is None:
                self.focus_default()
                return
            self.options[(cur.index + step) % len(self.options)].setFocus(Qt.TabFocusReason)

        def handle_key(self, ev) -> bool:
            k = ev.key()
            if k in (Qt.Key_Escape, Qt.Key_Backspace, Qt.Key_Back):
                self.request_close()
                return True
            if self.saving or self.done:
                return True
            if k in (Qt.Key_Left, Qt.Key_Backtab, Qt.Key_Up):
                self.move_focus(-1)
            elif k in (Qt.Key_Right, Qt.Key_Tab, Qt.Key_Down):
                self.move_focus(1)
            elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Select):
                cur = next((o for o in self.options if o.hasFocus()), None)
                if cur is not None:
                    self.choose(cur)
            elif Qt.Key_1 <= k <= Qt.Key_8:
                idx = k - Qt.Key_1
                if idx < len(self.options) and self.online:
                    self.options[idx].setFocus(Qt.ShortcutFocusReason)
                    self.choose(self.options[idx])
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

    main.window = bar  # for tests / debugging
    main.layered = layered
    try:
        return app.exec()
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
