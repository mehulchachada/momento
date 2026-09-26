"""Background recorder daemon: capture pipeline + ring buffer + control socket."""

from __future__ import annotations

import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, durations, protocol, quality, settings, storage

log = logging.getLogger(__name__)

STORAGE_CHECK_SECONDS = 30
# The resident clip bar is restarted after 1, 2, 4 ... 60 s; a bar that ran this
# long before exiting counts as healthy and starts the backoff over.
BAR_BACKOFF_MIN = 1.0
BAR_BACKOFF_MAX = 60.0
BAR_STABLE_SECONDS = 30.0


def notify(bus, summary: str, body: str = "", icon: str = config.APP_ID) -> None:
    """Fire-and-forget desktop notification via org.freedesktop.Notifications."""
    if bus is None:
        return
    try:
        import dbus

        obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
        iface = dbus.Interface(obj, "org.freedesktop.Notifications")
        iface.Notify(
            "Momento", dbus.UInt32(0), icon, summary, body,
            dbus.Array([], signature="s"),
            dbus.Dictionary({"desktop-entry": dbus.String(config.APP_ID)}, signature="sv"),
            dbus.Int32(5000),
            reply_handler=lambda *_: None,
            error_handler=lambda e: log.debug("notification failed: %s", e),
        )
    except Exception as e:  # noqa: BLE001 - notifications are best effort
        log.debug("notification failed: %s", e)


def spawn_overlay() -> None:
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "momento", "overlay"],
            # stderr stays attached so overlay crashes land in the daemon's journal.
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        # Reap it when it exits so it does not linger as a zombie.
        threading.Thread(target=proc.wait, daemon=True).start()
    except OSError as e:
        log.error("cannot launch overlay: %s", e)


class HourMarks:
    """Footage recorded in the current session, and the "hour" marks it crosses.

    A session starts with play from stopped (or the service starting) and runs
    through pauses; only footage counts, fed in segment by segment, so there is
    no clock to fake in tests. A mark is every ``length`` seconds of footage
    (``buffer.max_seconds``): from the first one on, the ring starts replacing
    the start of the session. ``add`` returns what to do:

    * ``("warn", seconds_left)``: ``warn`` seconds before a mark, once per mark.
      Before the first mark only, unless ``keep_history`` (then it announces
      every hour's save). Skipped when the lead is as long as the buffer.
    * ``("mark", offset)``: a mark was crossed ``offset`` seconds into the piece
      just added (so its wall-clock time is that segment's start + offset).
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.footage = 0.0
        self.marks = 0    # marks crossed so far
        self.warned = 0   # the highest mark warned about

    def add(self, seconds: float, length: float, warn: float, keep_history: bool) -> list[tuple]:
        events = []
        if length <= 0 or seconds <= 0:
            return events
        before = self.footage
        self.footage += seconds
        while self.footage >= (self.marks + 1) * length:
            self.marks += 1
            events.append(("mark", self.marks * length - before))
        nxt = (self.marks + 1) * length
        warn_at = nxt - warn
        if (self.warned <= self.marks and warn_at > self.marks * length and self.footage >= warn_at
                and (keep_history or self.marks == 0)):
            self.warned = self.marks + 1
            events.append(("warn", nxt - self.footage))
        return events


def span_label(seconds: float) -> str:
    """3600 -> "60 minutes", 60 -> "1 minute", 90 -> "1m30s"."""
    seconds = int(round(seconds))
    if seconds % 60:
        return durations.label(seconds)
    minutes = seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _private_bus():
    """A session-bus connection of our own, with no main loop (for blocking calls in a thread)."""
    try:
        import dbus
        import dbus.mainloop

        return dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
    except Exception:  # noqa: BLE001 - windowname then opens the bus itself
        log.debug("no private session bus", exc_info=True)
        return None


def _window_title(token: str) -> str | None:
    """The picked window's title from its restore token; None when unknown.

    windowname asks the portal's permission store over D-Bus (a few ms; it has its
    own timeout). This runs in a worker thread, so it gets a private connection
    without a main loop instead of the daemon's shared one. Imported here so a
    missing or broken module only costs the name.
    """
    bus = None
    try:
        from . import windowname

        bus = _private_bus()
        name = windowname.title_for_token(token, bus=bus)
    except Exception:  # noqa: BLE001 - the name is a nicety, never an error
        log.debug("window name lookup failed", exc_info=True)
        return None
    finally:
        if bus is not None:
            try:
                bus.close()
            except Exception:  # noqa: BLE001
                pass
    return name if isinstance(name, str) and name.strip() else None


def _popen_bar():
    """The resident clip bar: built once, hidden, shown by the hotkey (see overlay.py)."""
    return subprocess.Popen(
        [sys.executable, "-m", "momento", "overlay", "--resident"],
        # stderr stays attached so the bar's messages land in the daemon's journal.
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


class Daemon:
    def __init__(self, cfg: dict, loop, bus=None):
        from .ringbuffer import RingBuffer

        self.cfg = cfg
        self.loop = loop
        self.bus = bus
        self.buffer_dir = Path(cfg["buffer"]["dir"])
        # The buffer persists in buffer_dir (index.jsonl): it survives pause/resume,
        # setting changes, restarts and reboots. quit clears it, and so does a stop
        # unless [buffer] keep_history.
        self.ring = RingBuffer(cfg["buffer"]["max_seconds"], directory=self.buffer_dir)
        self.state = "starting"
        self.error: str | None = None
        self.recorder = None
        self.server = None
        self.shortcut = None
        self.paused = False
        # Stopped: paused, and the next play starts a new session. Set by Stop, by
        # the recorded window closing, and in window mode until the first play.
        # The history is cleared on a stop unless [buffer] keep_history.
        self.stopped = False
        self.stop_reason: str | None = None  # "user" | "window_closed" (status.stop_reason)
        self._stopping = False
        # The current session's footage and hour marks (see HourMarks).
        self.hours = HourMarks()
        self.ring.on_closed = self._on_segment_closed
        # Window mode: the picked window's title (status.target_name), looked up
        # off the main loop once per portal session.
        self.target_name: str | None = None
        self._name_token: str | None = None
        self._name_gen = 0
        # Disk-space guard: set while capture is blocked (state "no_storage").
        self.storage_error: str | None = None
        self._storage_reason: str | None = None  # "start" (never fit) | "low" (ran low while recording)
        self._storage_notified = False
        self._storage_timer = 0
        # Low-storage warning (status.storage.low): a full span at the current settings
        # doesn't fit. Notified once; re-armed when space or the need changes enough.
        self._low_notified = False
        self._low_need = None  # (required, history bytes) of the settings when it was sent
        self._low_disk = "buffer"
        # Resident clip bar ([ui] keep_bar_loaded).
        self.bar_proc = None
        self._bar_started = 0.0
        self._bar_backoff = BAR_BACKOFF_MIN
        self._bar_timer = 0
        self._bar_managed = False  # set by start(): only a started daemon runs the bar
        # Game controllers: watched for the "open the bar" chord only ([controller]).
        self.pads = None
        self._pads_handle = None
        self._pads_managed = False  # set by start(), like the bar

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        from . import ipc
        from .pipeline import Recorder

        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        # Pick up footage from before a restart/reboot (drops strays and the
        # unfinished last segment); saveable even if capture cannot start.
        self.ring.recover()
        self.server = ipc.Server(config.SOCKET_PATH, self.handle)
        self.server.start()
        if self.bus is not None:
            from .portal import register_app_id

            register_app_id(self.bus, config.APP_ID)
        self.recorder = Recorder(self.cfg, self.ring, self._on_state, bus=self.bus)
        self.hours.reset()
        if self._waits_for_play():
            # Window mode never records on its own at login (that would mean a
            # picker): it waits, stopped, until the user presses play.
            self.paused = self.stopped = True
            log.info("window mode: waiting for play to pick a window")
        else:
            self._start_recorder()  # our own buffer (if any is left) counts as reclaimable
        self._check_low()
        from gi.repository import GLib

        self._storage_timer = GLib.timeout_add_seconds(STORAGE_CHECK_SECONDS, self._storage_tick)
        if self.cfg["hotkey"].get("enabled") and self.bus is not None:
            try:
                from .hotkey import GlobalShortcut

                self.shortcut = GlobalShortcut(
                    self.bus, config.APP_ID, "save-replay", "Open Momento",
                    self.cfg["hotkey"].get("trigger", "LOGO+SHIFT+g"), self.open_bar,
                )
                self.shortcut.start()
            except Exception as e:  # noqa: BLE001
                log.warning("global shortcut unavailable: %s", e)
        self._bar_managed = True
        self.start_bar()
        self._pads_managed = True
        self._sync_controller()

    def stop(self, clear_buffer: bool = False) -> None:
        """Shut down. The buffer is kept (SIGTERM, service restart, reboot) unless
        clear_buffer is set, which only the explicit quit/stop command does."""
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down%s", " and clearing the replay buffer" if clear_buffer else "")
        if self._storage_timer:
            from gi.repository import GLib

            GLib.source_remove(self._storage_timer)
            self._storage_timer = 0
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:  # noqa: BLE001
                log.exception("recorder stop failed")
        if self.shortcut is not None and hasattr(self.shortcut, "close"):
            try:
                self.shortcut.close()
            except Exception:  # noqa: BLE001
                pass
        self.stop_bar()
        self._close_controller()
        if self.server is not None:
            self.server.close()
        if clear_buffer:
            self.ring.clear()
            _clean_dir(self.buffer_dir)
        if self.loop is not None:
            self.loop.quit()

    # --- the clip bar ---------------------------------------------------------------

    def keep_bar_loaded(self) -> bool:
        return bool(self.cfg.get("ui", {}).get("keep_bar_loaded", True))

    def open_bar(self) -> None:
        """Hotkey: toggle the resident bar; without one, start a one-shot bar as before.

        The toggle runs off the main loop so a slow or hung bar never stalls the recorder.
        """
        if not self.keep_bar_loaded():
            spawn_overlay()
            return
        threading.Thread(target=self._toggle_bar, name="bar-toggle", daemon=True).start()

    def _toggle_bar(self) -> None:
        from . import overlay

        try:
            if overlay.toggle():
                return
        except Exception:  # noqa: BLE001
            log.exception("cannot reach the clip bar")
        log.info("resident clip bar not reachable; starting a one-shot bar")
        spawn_overlay()

    def start_bar(self) -> None:
        """Start the resident bar once; _bar_exited restarts it if it dies."""
        if (not self._bar_managed or self._stopping or not self.keep_bar_loaded()
                or self.bar_proc is not None):
            return
        try:
            proc = _popen_bar()
        except OSError as e:
            log.error("cannot start the clip bar: %s", e)
            self._schedule_bar_restart()
            return
        self.bar_proc = proc
        self._bar_started = time.monotonic()
        threading.Thread(target=self._wait_bar, args=(proc,), name="bar-wait", daemon=True).start()

    def _wait_bar(self, proc) -> None:
        code = proc.wait()
        from gi.repository import GLib

        GLib.idle_add(self._bar_exited, proc, code)

    def _bar_exited(self, proc, code) -> bool:
        if proc is not self.bar_proc:
            return False  # a bar we stopped on purpose
        self.bar_proc = None
        if self._stopping or not self.keep_bar_loaded():
            return False
        if time.monotonic() - self._bar_started >= BAR_STABLE_SECONDS:
            self._bar_backoff = BAR_BACKOFF_MIN
        log.warning("clip bar exited (code %s); restarting it in %gs", code, self._bar_backoff)
        self._schedule_bar_restart()
        return False

    def _schedule_bar_restart(self) -> None:
        from gi.repository import GLib

        if self._bar_timer or self._stopping:
            return
        delay = self._bar_backoff
        self._bar_backoff = min(BAR_BACKOFF_MAX, delay * 2)
        self._bar_timer = GLib.timeout_add(int(delay * 1000), self._restart_bar)

    def _restart_bar(self) -> bool:
        self._bar_timer = 0
        self.start_bar()
        return False

    def stop_bar(self) -> None:
        if self._bar_timer:
            from gi.repository import GLib

            GLib.source_remove(self._bar_timer)
            self._bar_timer = 0
        proc, self.bar_proc = self.bar_proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _sync_bar(self) -> None:
        """After a reload: start or stop the resident bar to match [ui] keep_bar_loaded."""
        if not self._bar_managed:
            return
        if self.keep_bar_loaded():
            self._bar_backoff = BAR_BACKOFF_MIN
            self.start_bar()
        else:
            self.stop_bar()

    # --- game controllers ---------------------------------------------------------

    # Builds the hub; tests swap in one with fake devices.
    pad_factory = None

    def _sync_controller(self) -> None:
        """Watch the controllers for the chord while [controller] enabled (else let go).

        Chord only: the daemon never navigates and never grabs a controller; its hub
        reads key events alone, so stick movement in a game doesn't wake it up. While
        the bar is open it takes the controllers over, and then its own hub sees the
        chord (and closes it).
        """
        if not self._pads_managed:
            return
        ctl = config.controller(self.cfg)
        if not ctl["enabled"] or self._stopping:
            self._close_controller()
            return
        if self.pads is not None:
            self.pads.set_chord(ctl["chord"], ctl["hold_ms"])
            return
        from . import gamepad

        factory = self.pad_factory or gamepad.Gamepads
        hub = factory(navigate=False, chord=ctl["chord"], hold_ms=ctl["hold_ms"],
                      on_chord=self._on_pad_chord)
        try:
            started = hub.start()
        except Exception:  # noqa: BLE001 - a controller problem must not stop the recorder
            log.exception("controller support failed to start")
            started = False
        if not started:
            hub.close()
            return
        self.pads = hub
        try:
            self._pads_handle = hub.attach_glib()
        except Exception:  # noqa: BLE001 - no GLib main loop (tests)
            log.debug("controller hub not attached to a main loop", exc_info=True)
        log.info("controller shortcut: %s held %.1f s", " + ".join(ctl["chord"]), ctl["hold_ms"] / 1000)

    def _close_controller(self) -> None:
        hub, self.pads = self.pads, None
        handle, self._pads_handle = self._pads_handle, None
        if handle is not None:
            handle.detach()
        if hub is not None:
            hub.close()

    def _on_pad_chord(self) -> None:
        """The controller shortcut: open the bar, or close it (same as the hotkey)."""
        self.open_bar()

    def _on_state(self, state: str, detail: str | None) -> None:
        log.info("recorder state: %s%s", state, f" ({detail})" if detail else "")
        was = self.state
        self.state = state
        self.error = detail if state == "error" else None
        if state == "recording" and self._window_target():
            self._look_up_target_name()
        if state != "no_window":
            return
        # Window mode, capture ended without being asked to. Clients never see
        # "no_window": it becomes a stop (or a pause) here.
        if was == "recording":
            # The recorded window closed: exactly like pressing Stop. The recorder
            # has already finished the segment being written.
            cleared = self._stop_capture("window_closed")
            if cleared:
                body = "Recording stopped. Replay cleared (turn on Keep history in Settings to keep it)."
            else:
                body = "Recording stopped. Your replay is kept — open the bar to save it."
            notify(self.bus, "Momento: game closed", body, "dialog-information")
            return
        if self.stopped:
            return  # already stopped (e.g. the portal session closing after the window)
        # The picker was dismissed, or there was no window to restore: nothing new
        # was recorded. Back to where play was pressed: paused if this session
        # already has footage (play continues it), else stopped.
        self.paused = True
        self.stopped = self.hours.footage == 0
        log.info("no window to record; %s", "stopped" if self.stopped else "paused")

    def _window_target(self) -> bool:
        return config.capture_target(self.cfg["capture"]) == "window"

    def _waits_for_play(self) -> bool:
        """Window mode on a source that can record one window: never start at login."""
        source = str(self.cfg["capture"].get("source") or "auto")
        return self._window_target() and source in ("auto", "portal", "test")

    def keep_history(self) -> bool:
        return config.keep_history(self.cfg)

    def _stop_capture(self, reason: str) -> bool:
        """Stop recording but keep the service; return whether the history was cleared.

        Shared by the Stop command and the recorded window closing. Without
        [buffer] keep_history the replay is deleted, once the recorder has
        finished the segment it was writing (Recorder.stop drains it).
        """
        self.paused = True
        self.stopped = True
        self.stop_reason = reason
        if reason == "user" and self.recorder is not None:
            self.recorder.stop()
        cleared = not self.keep_history()
        if cleared:
            self.ring.clear()
        log.info("recording stopped (%s); replay history %s", reason, "cleared" if cleared else "kept")
        return cleared

    def _new_session(self) -> None:
        """Play from stopped: the hour marks count from zero again."""
        self.hours.reset()
        self.stop_reason = None

    # --- window name --------------------------------------------------------------

    # lookup(token) -> title or None; tests swap in a fake (the real one asks KWin).
    name_lookup = None

    def _forget_target_name(self) -> None:
        """A new window is being picked, or full screen is recorded: no name."""
        self.target_name = None
        self._name_token = None
        self._name_gen += 1

    def _look_up_target_name(self) -> None:
        """Once per portal session: the picked window's title, from its restore token."""
        try:
            token = config.portal_token_path("window").read_text().strip()
        except OSError:
            return
        if not token or token == self._name_token:
            return
        lookup = self.name_lookup
        if lookup is None:
            if self.bus is None or os.environ.get("MOMENTO_TEST_SANDBOX"):
                return  # no session bus (or a test): nobody to ask
            lookup = _window_title
        self._name_token = token
        self._name_gen += 1
        gen = self._name_gen

        def work() -> None:
            try:
                name = lookup(token)
            except Exception:  # noqa: BLE001
                log.debug("window name lookup failed", exc_info=True)
                name = None
            # A restored session may not resolve: then the name from before stays.
            if name and gen == self._name_gen:
                self.target_name = str(name)
                log.info("recording window: %s", self.target_name)

        threading.Thread(target=work, name="window-name", daemon=True).start()

    # --- the session's hour marks -----------------------------------------------------

    def _on_segment_closed(self, seg) -> None:
        """Every closed segment: count the session's footage; warn before, and save at, each mark."""
        length = float(self.ring.max_seconds)
        warn = config.warn_minutes(self.cfg) * 60
        keep = self.keep_history()
        for event in self.hours.add(seg.duration, length, warn, keep):
            if event[0] == "warn":
                self._warn_mark(event[1], keep)
            elif keep:
                self._save_hour(until=seg.start + event[1])

    def _warn_mark(self, seconds_left: float, keep: bool) -> None:
        length = self.ring.max_seconds
        minutes = max(1, math.ceil(seconds_left / 60 - 0.01))
        if keep:
            what = ("this hour is saved to Videos and a new hour starts" if length == 3600
                    else f"the last {span_label(length)} are saved to Videos and a new stretch starts")
        else:
            what = "the start of this session starts being replaced. Save anything you want from it now"
        notify(self.bus, f"Momento: {span_label(length)} almost full", f"In {minutes} min {what}.",
               "dialog-information")

    def _save_hour(self, until: float) -> None:
        """keep_history: export the footage since the previous mark, like a save (off the main loop)."""
        from . import exporter

        length = self.ring.max_seconds
        sel = self.ring.select_last(length, until=until)
        if sel is None:
            return
        what = "hour" if length == 3600 else span_label(length)
        need = sum(_size(s.path) for s in sel.segments) + storage.SAVE_MARGIN
        try:
            free = storage.free_bytes(self.cfg["output"]["dir"])
        except OSError as e:
            log.warning("cannot check free space for the hour: %s", e)
            free = need
        if free < need:
            self.ring.release(sel)
            log.warning("not saving the %s: needs %s, %s free", what, storage.human(need), storage.human(free))
            notify(self.bus, f"Momento: couldn't save the {what} — disk full",
                   f"Needs {storage.human(need)}, {storage.human(free)} free. Recording continues.",
                   "dialog-warning")
            return
        when = datetime.now()
        log.info("saving the last %s (%.0f s of footage)", what, sel.duration)

        def work() -> None:
            try:
                path = exporter.export(sel, exporter.output_path(self.cfg, sel.duration, when))
                result = {"ok": True, "path": str(path), "seconds": round(sel.duration, 2)}
            except Exception as e:  # noqa: BLE001
                log.exception("saving the %s failed", what)
                result = {"ok": False, "error": str(e) or e.__class__.__name__}
            finally:
                self.ring.release(sel)
            from gi.repository import GLib

            GLib.idle_add(self._finish_hour, result, what)

        threading.Thread(target=work, name="save-hour", daemon=True).start()

    def _finish_hour(self, result: dict, what: str) -> bool:
        if result.get("ok"):
            notify(self.bus, f"Saved the last {what} to Videos", result["path"], "media-record")
        else:
            notify(self.bus, f"Momento: couldn't save the {what}", result.get("error", ""), "dialog-error")
        return False

    # --- disk space ---------------------------------------------------------------

    def storage_check(self, cfg: dict | None = None, reclaimable: int | None = None) -> dict:
        """storage.check for ``cfg`` (default: the running one); our own buffer counts as reclaimable."""
        if reclaimable is None:
            reclaimable = storage.dir_bytes(self.buffer_dir)
        return storage.check(cfg or self.cfg, reclaimable)

    def _start_recorder(self, reclaimable: int | None = None, interactive: bool = False) -> bool:
        """Start capture if a full buffer fits on disk; otherwise enter "no_storage".

        ``interactive``: the user asked for this start (resume, pick_window, switching
        to window mode), so in window mode it may open the window picker.
        """
        chk = self.storage_check(reclaimable=reclaimable)
        if not chk["ok"]:
            self._block(storage.start_error(chk), "start")
            return False
        self._unblock()
        self.recorder.start(interactive=interactive)
        return True

    def _block(self, message: str, reason: str) -> None:
        self.state = "no_storage"
        self.error = None
        self.storage_error = message
        self._storage_reason = reason
        log.warning("capture blocked: %s", message)
        if not self._storage_notified:
            self._storage_notified = True
            body = message + ("\nRecording stopped; what was buffered can still be saved." if reason == "low"
                              else "\nFree up space; Momento starts by itself once it fits.")
            notify(self.bus, "Momento: not enough disk space", body, "dialog-warning")

    def _unblock(self) -> None:
        if self.state == "no_storage":
            self.state = "starting"
        self.storage_error = None
        self._storage_reason = None
        self._storage_notified = False

    def _storage_tick(self) -> bool:
        """Every STORAGE_CHECK_SECONDS: auto-start once space appears; stop when it runs low;
        warn once when a full span no longer fits (in every state, paused and stopped too)."""
        try:
            self._guard_capture()
        except Exception:  # noqa: BLE001 - the timer must keep running
            log.exception("storage check failed")
        if not self._stopping:
            self._check_low()
        return True

    def _guard_capture(self) -> None:
        if self.paused or self.recorder is None or self._stopping or self.state == "no_window":
            return  # (no_window: nothing is being written, and only the user restarts it)
        if self.state == "no_storage":
            # After a low-space stop, only restart once real free space is back:
            # the kept footage stays on disk, so it can't be counted as room to grow.
            reclaimable = 0 if self._storage_reason == "low" else None
            if self.storage_check(reclaimable=reclaimable)["ok"]:
                log.info("enough disk space again; starting capture")
                self._start_recorder(reclaimable=reclaimable)
            return
        free = storage.free_bytes(self.buffer_dir)
        if free < storage.LOW_WATER:
            try:
                self.recorder.stop()  # keeps the ring: saves still work
            except Exception:  # noqa: BLE001
                log.exception("recorder stop failed")
            self._block(f"Disk almost full: {storage.human(free)} free", "low")

    def _check_low(self) -> None:
        """Notify once when a full span at the current settings stops fitting.

        Runs at daemon start, after a settings change and on every storage tick.
        Re-armed once there is REARM_MARGIN more than needed (so free space
        wobbling around the line doesn't repeat it), or when a settings change
        moves the need and it fits again. While capture is blocked the
        "not enough disk space" notification has said it already.
        """
        try:
            chk = self.storage_check()
        except Exception:  # noqa: BLE001 - a warning must never break a command or the timer
            log.exception("low-storage check failed")
            return
        need = (storage.required_bytes(self.cfg), storage.history_bytes(self.cfg))  # what the settings ask
        if not chk["low"]:
            if self._low_notified and (need != self._low_need or self._clear_of_low(chk)):
                log.info("storage: room for a full %s again", storage.span(self.ring.max_seconds))
                self._low_notified = False
            return
        if self._low_notified:
            return
        self._low_notified, self._low_need, self._low_disk = True, need, chk["disk"]
        message = storage.low_message(chk, self.ring.max_seconds)
        log.warning("%s", message)
        if self.state == "no_storage" and self.storage_error:
            return  # blocked: _block has notified
        notify(self.bus, "Momento: low storage", message.removeprefix("Low storage: "), "dialog-warning")

    def _clear_of_low(self, chk: dict) -> bool:
        """Hysteresis: REARM_MARGIN more than needed on the disk that was short."""
        if self._low_disk == "output" and chk["disk"] != "output":
            # the clips disk recovered; this check describes the buffer disk, so look there
            out = storage.output_dir(self.cfg)
            need = storage.history_bytes(self.cfg) + storage.RESERVE
            return out is None or storage.free_bytes(out) >= need + storage.REARM_MARGIN
        return chk["available"] >= chk["needed"] + storage.REARM_MARGIN

    def _storage_status(self) -> dict:
        return dict(self.storage_check())

    # --- requests (main loop) ----------------------------------------------------

    def handle(self, msg: dict, reply) -> None:
        cmd = msg.get("cmd")
        if cmd == "status":
            reply(self.status())
        elif cmd == "save":
            self.save(msg, reply)
        elif cmd == "screenshot":
            self.screenshot(reply)
        elif cmd == "reload":
            self.reload(reply)
        elif cmd == "settings":
            self.settings(reply)
        elif cmd == "configure":
            self.configure(msg, reply)
        elif cmd == "pause":
            self.pause(reply)
        elif cmd == "resume":
            self.resume(reply)
        elif cmd == "pick_window":
            self.pick_window(reply)
        elif cmd == "stop":
            self.stop_recording(reply)
        elif cmd == "quit":
            # Shuts the service down (the bar's Stop uses "stop" instead). Clears the
            # replay buffer unless keep_buffer is set.
            clear = not msg.get("keep_buffer")
            reply({"ok": True, "buffer_cleared": clear})
            from gi.repository import GLib

            GLib.timeout_add(100, lambda: (self.stop(clear_buffer=clear), False)[1])
        else:
            reply({"ok": False, "error": f"unknown command {cmd!r}"})

    def _cfg_path(self) -> Path | None:
        return Path(self.cfg["_path"]) if self.cfg.get("_path") else None

    def reload(self, reply, interactive: bool = False) -> None:
        """Re-read the config file and restart capture with it (buffered footage is kept).

        While paused the new settings are loaded but capture stays off until resume.
        """
        from .pipeline import Recorder

        try:
            cfg = config.load(self._cfg_path())
            quality.resolution(cfg["capture"])
            quality.bitrate_kbps(cfg["capture"])
        except (OSError, ValueError) as e:
            reply({"ok": False, "error": f"config not applied: {e}"})
            return
        if self.recorder is not None:
            self.recorder.stop()
        if config.capture_target(cfg["capture"]) != config.capture_target(self.cfg["capture"]):
            self._forget_target_name()
        self.cfg = cfg
        self._sync_bar()
        self._sync_controller()
        self.recorder = Recorder(self.cfg, self.ring, self._on_state, bus=self.bus)
        started = False
        if not self.paused:
            started = self._start_recorder(interactive=interactive)
        self._check_low()
        result = {"ok": True, "restarted": started, "paused": self.paused,
                  "state": self._idle_state(), "storage": self._storage_status()}
        if self.state == "no_storage" and not self.paused:
            result["warning"] = self.storage_error
        reply(result)

    def settings(self, reply) -> None:
        """Current settings + choices + audio devices (pactl runs off the main loop)."""
        fallback = self.cfg
        path = self._cfg_path()

        def work() -> None:
            try:
                try:
                    cfg = config.load(path)  # what is saved, which the UI edits
                except (OSError, ValueError):
                    cfg = fallback
                result = settings.describe(cfg)
                result["storage"] = storage.requirements(cfg, storage.dir_bytes(storage.buffer_dir(cfg)))
            except Exception as e:  # noqa: BLE001
                log.exception("settings failed")
                result = {"ok": False, "error": str(e) or e.__class__.__name__}
            reply(result)

        threading.Thread(target=work, name="settings", daemon=True).start()

    def configure(self, msg: dict, reply) -> None:
        """Validate + save changed settings, then reload the recorder if anything changed."""
        changes = msg.get("changes")
        if not isinstance(changes, dict) or not changes:
            reply({"ok": False, "error": "changes must be a non-empty object"})
            return
        try:
            saved = config.load(self._cfg_path())
            new = settings.preview(saved, changes)
            quality.bitrate_kbps(new["capture"])
            chk = storage.check(new, storage.dir_bytes(self.buffer_dir))
        except (OSError, ValueError) as e:
            reply({"ok": False, "error": str(e)})
            return
        # Refuse settings that need more room than there is. A change that doesn't
        # raise the requirement (a smaller size, another mic) always goes through.
        if (not chk["ok"] and not msg.get("force")
                and storage.required_bytes(new) > storage.required_bytes(saved)):
            reply({"ok": False, "code": "no_storage", "storage": chk,
                   "error": f"{storage.label(new)} needs {storage.human(chk['required'])} free, "
                            f"{storage.human(chk['free'] + chk['reclaimable'])} available"})
            return
        try:
            changed = settings.apply(changes, self._cfg_path())
        except (OSError, ValueError) as e:
            reply({"ok": False, "error": str(e)})
            return
        if not changed or set(changed) <= set(settings.LIVE_KEYS):
            # Controller, history and bar settings take effect without restarting
            # the recording.
            if changed:
                self._apply_live(changed)
                self._check_low()  # keep_history adds (or drops) a saved hour
            reply({"ok": True, "changed": changed, "restarted": False, "paused": self.paused,
                   "state": self._idle_state(), "storage": self._storage_status()})
            return
        # Switching what is recorded is the user's choice: in window mode the new
        # session may open the window picker (a new portal session either way).
        self.reload(lambda r: reply({**r, "changed": changed}), interactive="record" in changed)

    def _apply_live(self, changed: dict) -> None:
        """Take the saved values of settings in settings.LIVE_KEYS into the running config."""
        try:
            saved = config.load(self._cfg_path())
        except (OSError, ValueError) as e:
            log.warning("settings not reloaded: %s", e)
            return
        if set(changed) & set(settings.CONTROLLER_KEYS):
            self.cfg["controller"] = saved["controller"]
            self._sync_controller()
        for key in ("keep_history", "warn_minutes"):
            self.cfg["buffer"][key] = saved["buffer"].get(key, config.DEFAULTS["buffer"][key])
        if "instant_bar" in changed:
            self.cfg.setdefault("ui", {})["keep_bar_loaded"] = saved["ui"].get("keep_bar_loaded", True)
            self._sync_bar()

    def pause(self, reply) -> None:
        """Stop capturing but keep what is buffered; saves keep working on it."""
        if not self.paused:
            self.paused = True
            if self.recorder is not None:
                self.recorder.stop()
        reply({"ok": True, "state": self._idle_state()})

    def stop_recording(self, reply) -> None:
        """The bar's Stop: end recording, but keep the service (and with it the global
        shortcut) running so the bar still opens. The replay history is cleared
        unless [buffer] keep_history is on."""
        cleared = self._stop_capture("user")
        reply({"ok": True, "state": "stopped", "buffer_cleared": cleared})

    def _idle_state(self) -> str:
        return "stopped" if self.stopped else "paused" if self.paused else self.state

    def resume(self, reply) -> None:
        """Play: start capturing again; the footage from before stays.

        From paused it continues the session (window mode: the stored window is
        restored, or the portal asks). From stopped it starts a new session, and
        in window mode that means picking the window again: the stored one is
        forgotten, so the picker opens.
        """
        # Also retry after an error (e.g. the screen-share prompt was dismissed),
        # so the bar's play button is always a way back to recording.
        if self.paused or self.state in ("no_storage", "error", "no_window"):
            if self.stopped:
                self._new_session()
                if self._window_target():
                    config.forget_portal_token("window")
                    self._forget_target_name()
            self.paused = False
            self.stopped = False
            if self.recorder is not None and not self._start_recorder(interactive=True):
                reply({"ok": False, "code": "no_storage", "error": self.storage_error,
                       "state": self.state, "storage": self._storage_status()})
                return
        reply({"ok": True, "state": self.state})

    def pick_window(self, reply) -> None:
        """Window mode: forget the stored window and ask for one (the window picker opens).

        The bar's "Change window". Footage recorded so far is kept; a pause or stop
        is left, since picking a window means "record this" (from stopped, as a new
        session, like play).
        """
        if config.capture_target(self.cfg["capture"]) != "window":
            reply({"ok": False, "error": "Record is set to Full screen; choose Window first"})
            return
        config.forget_portal_token("window")
        self._forget_target_name()
        if self.stopped:
            self._new_session()
        self.paused = False
        self.stopped = False
        if self.recorder is not None:
            self.recorder.stop()
            if not self._start_recorder(interactive=True):
                reply({"ok": False, "code": "no_storage", "error": self.storage_error,
                       "state": self.state, "storage": self._storage_status()})
                return
        reply({"ok": True, "state": self.state})

    def status(self) -> dict:
        rec = self.recorder
        result = {
            "ok": True,
            "protocol": protocol.PROTOCOL_VERSION,
            "state": self._idle_state(),
            "recording": False if self.paused else bool(getattr(rec, "recording", False)),
            "buffered": round(self.ring.buffered_seconds(), 2),
            # Display only: also counts the piece being recorded right now, so the
            # bar's timer ticks every second. Saves still use "buffered".
            "buffered_live": round(self.ring.buffered_seconds(
                live=not self.paused and bool(getattr(rec, "recording", False))), 2),
            "max_seconds": self.ring.max_seconds,
            "source": getattr(rec, "source_name", None),
            "encoder": getattr(rec, "encoder_name", None),
            "output_dir": self.cfg["output"]["dir"],
            "target": config.capture_target(self.cfg["capture"]),
            # Window mode: the picked window's title, when the desktop tells us.
            "target_name": self.target_name if self._window_target() else None,
            # Why it is stopped: "user" (Stop), "window_closed", or null (not stopped
            # by either, e.g. window mode waiting for the first play).
            "stop_reason": self.stop_reason if self.stopped else None,
            "keep_history": self.keep_history(),
            "resolution": self.cfg["capture"]["resolution"],
            "quality": self.cfg["capture"]["quality"],
            "bitrate_kbps": quality.bitrate_kbps(self.cfg["capture"]),
            "fps": quality.fps(self.cfg["capture"]),
            "storage": self._storage_status(),
        }
        if self.state == "no_storage" and not self.paused and self.storage_error:
            result["error"] = self.storage_error
        elif self.error:
            result["error"] = self.error
        return result

    def save(self, msg: dict, reply) -> None:
        try:
            seconds = msg.get("seconds")
            if isinstance(seconds, bool):
                raise ValueError("seconds must be a number or a duration like \"5m\"")
            seconds = durations.parse(seconds) if isinstance(seconds, str) else int(seconds)
            if not 1 <= seconds <= durations.MAX_SECONDS:
                raise ValueError(f"duration must be between 1s and {durations.MAX_SECONDS}s")
        except (TypeError, ValueError) as e:
            reply({"ok": False, "error": f"bad duration: {e}"})
            return
        t_req = time.time()
        until = msg.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and t_req - 3600 < until < t_req:
            # The clip bar asks to end the clip when it was opened, so the bar
            # itself (visible on screen since then) isn't in the clip.
            t_req = float(until)
        when = datetime.now()
        done = []

        def after_flush(*_args) -> None:
            if done:
                return
            done.append(True)
            self._export(seconds, t_req, when, reply)

        if self.recorder is not None and getattr(self.recorder, "recording", False):
            try:
                self.recorder.flush(after_flush)
                return
            except Exception:  # noqa: BLE001
                log.exception("flush failed; saving what is already closed")
        after_flush()

    def _export(self, seconds: int, t_req: float, when: datetime, reply) -> None:
        from . import exporter

        # The newest `seconds` of footage, across pauses/restarts, ending at the request.
        sel = self.ring.select_last(seconds, until=t_req)
        if sel is None:
            result = {"ok": False, "error": "nothing recorded yet"}
            notify(self.bus, "Momento: nothing to save", "Nothing has been recorded yet.", "dialog-warning")
            reply(result)
            return
        need = sum(_size(s.path) for s in sel.segments) + storage.SAVE_MARGIN
        try:
            free = storage.free_bytes(self.cfg["output"]["dir"])
        except OSError as e:
            log.warning("cannot check free space for the clip: %s", e)
            free = need
        if free < need:
            self.ring.release(sel)
            error = (f"Not enough space to save this clip: needs {storage.human(need)}, "
                     f"{storage.human(free)} free")
            log.warning("%s", error)
            notify(self.bus, "Momento: not enough disk space", error, "dialog-warning")
            reply({"ok": False, "error": error, "code": "no_storage"})
            return

        def work() -> None:
            try:
                out = exporter.output_path(self.cfg, sel.duration, when)
                path = exporter.export(sel, out)
                result = {
                    "ok": True, "path": str(path), "seconds": round(sel.duration, 2),
                    "requested": seconds, "partial": sel.duration < seconds - 1.5,
                }
                if sel.note:  # e.g. "earlier footage used a different resolution"
                    result["reason"] = sel.note
            except Exception as e:  # noqa: BLE001
                log.exception("export failed")
                result = {"ok": False, "error": str(e) or e.__class__.__name__}
            finally:
                self.ring.release(sel)
            from gi.repository import GLib

            GLib.idle_add(self._finish_save, result)
            reply(result)

        threading.Thread(target=work, name="export", daemon=True).start()

    def screenshot(self, reply) -> None:
        """Save the next recorded frame as a PNG in <output dir>/Images (only while recording).

        The frame is one captured after this request arrived, so a client that got
        out of the way first (the clip bar hides itself) is not in the picture.
        """
        from . import screenshot

        rec = self.recorder
        if self.paused or rec is None or not getattr(rec, "recording", False):
            reply({"ok": False, "code": "not_recording",
                   "error": "Not recording — screenshots are taken from the recording"})
            return
        out_dir = screenshot.images_dir(self.cfg)
        try:
            free = storage.free_bytes(out_dir)
        except OSError as e:
            log.warning("cannot check free space for the screenshot: %s", e)
            free = storage.SAVE_MARGIN
        if free < storage.SAVE_MARGIN:
            error = f"Not enough space to save a screenshot: {storage.human(free)} free"
            notify(self.bus, "Momento: not enough disk space", error, "dialog-warning")
            reply({"ok": False, "code": "no_storage", "error": error})
            return
        when = datetime.now()
        cfg = self.cfg

        def got(frame) -> None:
            if frame is None:
                self._finish_screenshot(reply, {"ok": False, "error": "No picture came from the recording"})
                return

            def work() -> None:
                try:
                    path = screenshot.save(frame, cfg, when)
                    w, h = frame.size
                    result = {"ok": True, "path": str(path), "width": w, "height": h}
                except Exception as e:  # noqa: BLE001
                    log.exception("screenshot failed")
                    result = {"ok": False, "error": str(e) or e.__class__.__name__}
                self._finish_screenshot(reply, result)

            threading.Thread(target=work, name="screenshot", daemon=True).start()

        try:
            rec.grab_frame(got)
        except Exception as e:  # noqa: BLE001
            log.exception("screenshot failed")
            self._finish_screenshot(reply, {"ok": False, "error": str(e) or e.__class__.__name__})

    def _finish_screenshot(self, reply, result: dict) -> None:
        """Any thread: reply, and notify from the main loop (as a save does)."""
        from gi.repository import GLib

        if not result.get("ok"):
            log.warning("screenshot failed: %s", result.get("error"))
        GLib.idle_add(self._notify_screenshot, result)
        reply(result)

    def _notify_screenshot(self, result: dict) -> bool:
        # The clip bar hides itself before asking, so the notification is the feedback.
        if result.get("ok"):
            notify(self.bus, "Screenshot saved", result["path"], "camera-photo")
        else:
            notify(self.bus, "Momento: screenshot failed", result.get("error", ""), "dialog-error")
        return False

    def _finish_save(self, result: dict) -> bool:
        if result.get("ok"):
            got = durations.label(result["seconds"])
            summary = f"Saved last {got}"
            if result["partial"]:
                summary += f" (asked for {durations.label(result['requested'])})"
            body = result["path"] + (f"\n{result['reason'].capitalize()}." if result.get("reason") else "")
            notify(self.bus, summary, body, "media-record")
        else:
            notify(self.bus, "Momento: save failed", result.get("error", ""), "dialog-error")
        return False


def _size(path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _clean_dir(path: Path) -> None:
    """Remove leftover segments. Only touches *.ts files in our own buffer dir."""
    if not path.is_dir():
        return
    for p in path.iterdir():
        if p.is_file() and p.suffix in (".ts", ".tmp"):
            try:
                p.unlink()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass


def main(cfg: dict) -> int:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SessionBus()
    except dbus.DBusException as e:
        log.warning("no session bus (%s); hotkey and notifications disabled", e)
        bus = None
    loop = GLib.MainLoop()
    daemon = Daemon(cfg, loop, bus)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: (daemon.stop(), False)[1])
    try:
        daemon.start()
    except Exception as e:  # noqa: BLE001
        log.error("daemon failed to start: %s", e)
        daemon.stop()
        return 1
    log.info("Momento daemon running (socket %s)", config.SOCKET_PATH)
    try:
        loop.run()
    finally:
        daemon.stop()
    return 0
