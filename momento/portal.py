"""xdg-desktop-portal ScreenCast client (dbus-python, GLib main loop).

Flow: CreateSession -> SelectSources -> Start -> OpenPipeWireRemote, each step
answered asynchronously through an org.freedesktop.portal.Request "Response"
signal. The restore token is persisted so later sessions start without the
picker dialog (persist_mode=2, "until explicitly revoked").
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Callable

import dbus

log = logging.getLogger("momento.portal")

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

SOURCE_MONITOR = 1
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2
PERSIST_PERSISTENT = 2

_RESPONSE_TEXT = {1: "cancelled", 2: "failed"}


def register_app_id(bus: dbus.Bus, app_id: str) -> None:
    """Tell xdg-desktop-portal which app this connection is (host apps only).

    Must happen before any portal session is created: registering later makes
    the portal reject sessions opened under the old, empty app id
    ("Invalid session"). Older portals lack the interface; that is fine.
    """
    try:
        registry = dbus.Interface(
            bus.get_object(BUS_NAME, OBJECT_PATH, follow_name_owner_changes=True),
            "org.freedesktop.host.portal.Registry",
        )
        registry.Register(app_id, dbus.Dictionary({}, signature="sv"), timeout=3)
    except dbus.DBusException as e:
        log.debug("host portal Registry.Register: %s", e.get_dbus_message())


class ScreenCastPortal:
    def __init__(self, bus: dbus.Bus, token_path: Path, cursor: bool):
        self.bus = bus
        self.token_path = Path(token_path)
        self.cursor = cursor
        self.session_handle: str | None = None
        self.node_id: int | None = None
        self._sender = bus.get_unique_name().lstrip(":").replace(".", "_")
        self._obj = bus.get_object(BUS_NAME, OBJECT_PATH)
        self._iface = dbus.Interface(self._obj, SCREENCAST_IFACE)
        self._signal_matches: list = []
        self._on_ready: Callable[[int, int], None] | None = None
        self._on_error: Callable[[str], None] | None = None
        self._done = False
        self._closed = False

    # --- public ---------------------------------------------------------------

    def start(self, on_ready: Callable[[int, int], None], on_error: Callable[[str], None]) -> None:
        self._on_ready, self._on_error = on_ready, on_error
        self._done = False
        self._closed = False
        session_token = self._new_token("momento_s")
        self._request(
            "CreateSession",
            self._on_session_created,
            {"session_handle_token": dbus.String(session_token)},
        )

    def close(self) -> None:
        self._closed = True
        for match in self._signal_matches:
            try:
                match.remove()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
        self._signal_matches.clear()
        if self.session_handle:
            try:
                session = dbus.Interface(self.bus.get_object(BUS_NAME, self.session_handle), SESSION_IFACE)
                session.Close(reply_handler=lambda: None, error_handler=lambda e: None)
            except dbus.DBusException as e:
                log.debug("session close failed: %s", e)
            self.session_handle = None

    # --- steps ------------------------------------------------------------------

    def _on_session_created(self, results: dict) -> None:
        self.session_handle = str(results["session_handle"])
        log.debug("portal session %s", self.session_handle)
        self._signal_matches.append(
            self.bus.add_signal_receiver(
                self._on_session_closed,
                signal_name="Closed",
                dbus_interface=SESSION_IFACE,
                path=self.session_handle,
            )
        )
        version = self._prop("version", 1)
        cursor_modes = self._prop("AvailableCursorModes", CURSOR_HIDDEN)
        cursor_mode = CURSOR_EMBEDDED if self.cursor and int(cursor_modes) & CURSOR_EMBEDDED else CURSOR_HIDDEN
        options = {
            "types": dbus.UInt32(SOURCE_MONITOR),
            "multiple": dbus.Boolean(False),
        }
        if int(cursor_modes) & cursor_mode:
            options["cursor_mode"] = dbus.UInt32(cursor_mode)
        if int(version) >= 4:
            options["persist_mode"] = dbus.UInt32(PERSIST_PERSISTENT)
            token = self._load_token()
            if token:
                options["restore_token"] = dbus.String(token)
        self._request("SelectSources", self._on_sources_selected, options, dbus.ObjectPath(self.session_handle))

    def _on_sources_selected(self, results: dict) -> None:
        self._request("Start", self._on_started, {}, dbus.ObjectPath(self.session_handle), "")

    def _on_started(self, results: dict) -> None:
        token = results.get("restore_token")
        if token:
            self._save_token(str(token))
        streams = results.get("streams") or []
        if not streams:
            self._fail("portal returned no streams")
            return
        self.node_id = int(streams[0][0])
        self._iface.OpenPipeWireRemote(
            dbus.ObjectPath(self.session_handle),
            dbus.Dictionary({}, signature="sv"),
            reply_handler=self._on_remote,
            error_handler=lambda e: self._fail(f"OpenPipeWireRemote: {e.get_dbus_message()}"),
        )

    def _on_remote(self, fd) -> None:
        if self._closed:
            return
        fd = fd.take() if hasattr(fd, "take") else int(fd)
        self._done = True
        log.info("portal ready: pipewire fd=%d node=%d", fd, self.node_id)
        if self._on_ready:
            self._on_ready(fd, self.node_id)

    def _on_session_closed(self, *args) -> None:
        log.info("portal session closed by compositor")
        self.session_handle = None
        if not self._closed:
            self._fail("session closed")

    # --- plumbing ---------------------------------------------------------------

    def _request(self, method: str, on_response: Callable[[dict], None], options: dict, *args) -> None:
        """Call a portal method that answers through a Request object.

        We subscribe to the Response signal on the predicted request path
        *before* the call so a fast reply cannot be missed.
        """
        if self._closed:
            return
        token = self._new_token("momento_r")
        path = f"{OBJECT_PATH}/request/{self._sender}/{token}"
        holder: dict = {}

        def on_signal(code, results):
            match = holder.pop("match", None)
            if match is not None:
                match.remove()
                if match in self._signal_matches:
                    self._signal_matches.remove(match)
            if self._closed:
                return
            code = int(code)
            if code != 0:
                self._fail(_RESPONSE_TEXT.get(code, f"{method} error {code}"))
                return
            try:
                on_response(dict(results))
            except Exception as e:  # noqa: BLE001 - report instead of dying in a D-Bus callback
                log.exception("portal %s handling failed", method)
                self._fail(f"{method}: {e}")

        match = self.bus.add_signal_receiver(
            on_signal, signal_name="Response", dbus_interface=REQUEST_IFACE, path=path
        )
        holder["match"] = match
        self._signal_matches.append(match)
        options = dict(options)
        options["handle_token"] = dbus.String(token)
        getattr(self._iface, method)(
            *args,
            dbus.Dictionary(options, signature="sv"),
            reply_handler=lambda handle: log.debug("%s -> %s", method, handle),
            error_handler=lambda e: self._fail(f"{method}: {e.get_dbus_message()}"),
        )

    def _prop(self, name: str, default):
        try:
            props = dbus.Interface(self._obj, "org.freedesktop.DBus.Properties")
            return props.Get(SCREENCAST_IFACE, name)
        except dbus.DBusException:
            return default

    def _fail(self, message: str) -> None:
        if self._closed:
            return
        log.warning("screencast portal: %s", message)
        cb = self._on_error
        self.close()
        if cb:
            cb(message)

    @staticmethod
    def _new_token(prefix: str) -> str:
        return f"{prefix}{secrets.token_hex(6)}"

    def _load_token(self) -> str | None:
        try:
            return self.token_path.read_text().strip() or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.token_path.with_suffix(".tmp")
            tmp.write_text(token)
            os.chmod(tmp, 0o600)
            tmp.replace(self.token_path)
        except OSError as e:
            log.warning("could not save portal restore token: %s", e)
