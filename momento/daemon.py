"""Background recorder daemon: capture pipeline + ring buffer + control socket."""

from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, durations, quality, settings, storage

log = logging.getLogger(__name__)

STORAGE_CHECK_SECONDS = 30


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


class Daemon:
    def __init__(self, cfg: dict, loop, bus=None):
        from .ringbuffer import RingBuffer

        self.cfg = cfg
        self.loop = loop
        self.bus = bus
        self.buffer_dir = Path(cfg["buffer"]["dir"])
        # The buffer persists in buffer_dir (index.jsonl): it survives pause/resume,
        # setting changes, restarts and reboots. Only an explicit quit clears it.
        self.ring = RingBuffer(cfg["buffer"]["max_seconds"], directory=self.buffer_dir)
        self.state = "starting"
        self.error: str | None = None
        self.recorder = None
        self.server = None
        self.shortcut = None
        self.paused = False
        self._stopping = False
        # Disk-space guard: set while capture is blocked (state "no_storage").
        self.storage_error: str | None = None
        self._storage_reason: str | None = None  # "start" (never fit) | "low" (ran low while recording)
        self._storage_notified = False
        self._storage_timer = 0

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
        self._start_recorder()  # our own buffer (if any is left) counts as reclaimable
        from gi.repository import GLib

        self._storage_timer = GLib.timeout_add_seconds(STORAGE_CHECK_SECONDS, self._storage_tick)
        if self.cfg["hotkey"].get("enabled") and self.bus is not None:
            try:
                from .hotkey import GlobalShortcut

                self.shortcut = GlobalShortcut(
                    self.bus, config.APP_ID, "save-replay", "Open Momento",
                    self.cfg["hotkey"].get("trigger", "LOGO+SHIFT+g"), spawn_overlay,
                )
                self.shortcut.start()
            except Exception as e:  # noqa: BLE001
                log.warning("global shortcut unavailable: %s", e)

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
        if self.server is not None:
            self.server.close()
        if clear_buffer:
            self.ring.clear()
            _clean_dir(self.buffer_dir)
        if self.loop is not None:
            self.loop.quit()

    def _on_state(self, state: str, detail: str | None) -> None:
        log.info("recorder state: %s%s", state, f" ({detail})" if detail else "")
        self.state = state
        self.error = detail if state == "error" else None

    # --- disk space ---------------------------------------------------------------

    def storage_check(self, cfg: dict | None = None, reclaimable: int | None = None) -> dict:
        """storage.check for ``cfg`` (default: the running one); our own buffer counts as reclaimable."""
        if reclaimable is None:
            reclaimable = storage.dir_bytes(self.buffer_dir)
        return storage.check(cfg or self.cfg, reclaimable)

    def _start_recorder(self, reclaimable: int | None = None) -> bool:
        """Start capture if a full buffer fits on disk; otherwise enter "no_storage"."""
        chk = self.storage_check(reclaimable=reclaimable)
        if not chk["ok"]:
            self._block(storage.start_error(chk), "start")
            return False
        self._unblock()
        self.recorder.start()
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
        """Every STORAGE_CHECK_SECONDS: auto-start once space appears; stop when it runs low."""
        try:
            if self.paused or self.recorder is None or self._stopping:
                return True
            if self.state == "no_storage":
                # After a low-space stop, only restart once real free space is back:
                # the kept footage stays on disk, so it can't be counted as room to grow.
                reclaimable = 0 if self._storage_reason == "low" else None
                if self.storage_check(reclaimable=reclaimable)["ok"]:
                    log.info("enough disk space again; starting capture")
                    self._start_recorder(reclaimable=reclaimable)
                return True
            free = storage.free_bytes(self.buffer_dir)
            if free < storage.LOW_WATER:
                try:
                    self.recorder.stop()  # keeps the ring: saves still work
                except Exception:  # noqa: BLE001
                    log.exception("recorder stop failed")
                self._block(f"Disk almost full: {storage.human(free)} free", "low")
        except Exception:  # noqa: BLE001 - the timer must keep running
            log.exception("storage check failed")
        return True

    def _storage_status(self) -> dict:
        chk = self.storage_check()
        return {k: chk[k] for k in ("ok", "free", "required", "reclaimable", "path")}

    # --- requests (main loop) ----------------------------------------------------

    def handle(self, msg: dict, reply) -> None:
        cmd = msg.get("cmd")
        if cmd == "status":
            reply(self.status())
        elif cmd == "save":
            self.save(msg, reply)
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
        elif cmd == "quit":
            # The explicit Stop clears the replay buffer unless keep_buffer is set.
            clear = not msg.get("keep_buffer")
            reply({"ok": True, "buffer_cleared": clear})
            from gi.repository import GLib

            GLib.timeout_add(100, lambda: (self.stop(clear_buffer=clear), False)[1])
        else:
            reply({"ok": False, "error": f"unknown command {cmd!r}"})

    def _cfg_path(self) -> Path | None:
        return Path(self.cfg["_path"]) if self.cfg.get("_path") else None

    def reload(self, reply) -> None:
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
        self.cfg = cfg
        self.recorder = Recorder(self.cfg, self.ring, self._on_state, bus=self.bus)
        started = False
        if not self.paused:
            started = self._start_recorder()
        result = {"ok": True, "restarted": started, "paused": self.paused,
                  "state": "paused" if self.paused else self.state, "storage": self._storage_status()}
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
        if not changed:
            reply({"ok": True, "changed": {}, "restarted": False, "paused": self.paused,
                   "state": "paused" if self.paused else self.state, "storage": self._storage_status()})
            return
        self.reload(lambda r: reply({**r, "changed": changed}))

    def pause(self, reply) -> None:
        """Stop capturing but keep what is buffered; saves keep working on it."""
        if not self.paused:
            self.paused = True
            if self.recorder is not None:
                self.recorder.stop()
        reply({"ok": True, "state": "paused"})

    def resume(self, reply) -> None:
        """Start capturing again as a new session; the footage from before the pause stays."""
        if self.paused or self.state == "no_storage":
            self.paused = False
            if self.recorder is not None and not self._start_recorder():
                reply({"ok": False, "code": "no_storage", "error": self.storage_error,
                       "state": self.state, "storage": self._storage_status()})
                return
        reply({"ok": True, "state": self.state})

    def status(self) -> dict:
        rec = self.recorder
        result = {
            "ok": True,
            "state": "paused" if self.paused else self.state,
            "recording": False if self.paused else bool(getattr(rec, "recording", False)),
            "buffered": round(self.ring.buffered_seconds(), 2),
            "max_seconds": self.ring.max_seconds,
            "source": getattr(rec, "source_name", None),
            "encoder": getattr(rec, "encoder_name", None),
            "output_dir": self.cfg["output"]["dir"],
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
            seconds = durations.parse(seconds) if isinstance(seconds, str) else int(seconds)
            if not 1 <= seconds <= durations.MAX_SECONDS:
                raise ValueError(f"duration must be between 1s and {durations.MAX_SECONDS}s")
        except (TypeError, ValueError) as e:
            reply({"ok": False, "error": f"bad duration: {e}"})
            return
        t_req = time.time()
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
