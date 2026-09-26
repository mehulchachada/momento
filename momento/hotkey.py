"""Global hotkey via the xdg-desktop-portal GlobalShortcuts interface.

Works on KDE Plasma 6 (portal interface version 2) and GNOME 48+. Everything
is asynchronous on the GLib main loop: every portal call returns a Request
object path whose ``Response`` signal carries the real result, so we
subscribe to that path *before* making the call (the path is predictable from
our unique bus name and the ``handle_token`` we choose).

If the portal (or the GlobalShortcuts interface) is missing, a warning is
logged and nothing else happens -- the user can bind ``momento overlay`` to
a key in the desktop's own shortcut settings instead.
"""

from __future__ import annotations

import logging
import secrets
from typing import Callable

import dbus

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
IFACE_REQUEST = "org.freedesktop.portal.Request"
IFACE_SESSION = "org.freedesktop.portal.Session"
IFACE_REGISTRY = "org.freedesktop.host.portal.Registry"

FALLBACK_HINT = (
    "Global hotkey unavailable ({reason}). Fallback: bind the command "
    "`momento overlay` to a key in your desktop's shortcut settings "
    "(KDE: System Settings -> Keyboard -> Shortcuts -> Add New -> Command)."
)


def _token(prefix: str = "momento") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class GlobalShortcut:
    """One portal-registered global shortcut.

    ``trigger`` uses the XDG shortcuts spec syntax, e.g. ``"LOGO+SHIFT+g"`` or
    ``"CTRL+ALT+r"``; it is only a *preferred* trigger -- the desktop may show
    a dialog and the user can pick something else.
    """

    def __init__(self, bus: dbus.Bus, app_id: str, shortcut_id: str,
                 description: str, trigger: str,
                 on_activated: Callable[[], None]):
        self.bus = bus
        self.app_id = app_id
        self.shortcut_id = shortcut_id
        self.description = description
        self.trigger = trigger
        self.on_activated = on_activated
        self.session_handle: str | None = None
        self.active = False
        self._matches: list = []
        self._portal = None
        self._closed = False

    # ------------------------------------------------------------------ util
    def _sender_part(self) -> str:
        return self.bus.get_unique_name().lstrip(":").replace(".", "_")

    def _request_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/request/{self._sender_part()}/{token}"

    def _call(self, method: str, args: list, on_response: Callable[[int, dict], None]):
        """Call a portal method that returns a Request handle.

        ``args``' last element must be the options dict; a handle_token is
        added to it. ``on_response(code, results)`` fires on the Response
        signal (code 0 = success, 1 = cancelled, 2 = other error).
        """
        token = _token("req")
        options = dict(args[-1])
        options["handle_token"] = token
        args = list(args[:-1]) + [dbus.Dictionary(options, signature="sv")]
        expected = self._request_path(token)
        holder: dict = {}

        def handler(code, results):
            m = holder.pop("match", None)
            if m is not None:
                m.remove()
                if m in self._matches:
                    self._matches.remove(m)
            if self._closed:
                return
            try:
                on_response(int(code), dict(results))
            except Exception:  # never let a callback kill the main loop
                log.exception("GlobalShortcuts: %s response handler failed", method)

        match = self.bus.add_signal_receiver(
            handler, signal_name="Response", dbus_interface=IFACE_REQUEST,
            bus_name=PORTAL_BUS, path=expected)
        holder["match"] = match
        self._matches.append(match)

        def on_reply(handle):
            if str(handle) != expected:
                # Very old portals used a different path; follow the real one.
                match.remove()
                holder["match"] = self.bus.add_signal_receiver(
                    handler, signal_name="Response", dbus_interface=IFACE_REQUEST,
                    bus_name=PORTAL_BUS, path=str(handle))
                self._matches.append(holder["match"])

        def on_error(err):
            m = holder.pop("match", None)
            if m is not None:
                m.remove()
            self._fail(f"{method} failed: {err.get_dbus_message() if hasattr(err, 'get_dbus_message') else err}")

        getattr(self._portal, method)(*args, dbus_interface=IFACE_SHORTCUTS,
                                      reply_handler=on_reply, error_handler=on_error)

    def _fail(self, reason: str):
        log.warning(FALLBACK_HINT.format(reason=reason))

    # ----------------------------------------------------------------- flow
    def start(self) -> None:
        """Begin registration asynchronously. Never raises."""
        try:
            self._start()
        except Exception as e:  # noqa: BLE001
            self._fail(str(e))

    def _start(self):
        try:
            # follow_name_owner_changes avoids a *synchronous* StartServiceByName
            # here; the async calls below still auto-activate the portal.
            self._portal = self.bus.get_object(PORTAL_BUS, PORTAL_PATH, introspect=False,
                                               follow_name_owner_changes=True)
        except dbus.DBusException as e:
            self._fail(f"xdg-desktop-portal not reachable: {e.get_dbus_message()}")
            return
        props = dbus.Interface(self._portal, "org.freedesktop.DBus.Properties")

        def have_version(version):
            log.debug("GlobalShortcuts portal version %s", int(version))
            self._register_app()

        def no_iface(err):
            self._fail("the desktop portal has no GlobalShortcuts interface")

        props.Get(IFACE_SHORTCUTS, "version",
                  reply_handler=have_version, error_handler=no_iface)

    def _register_app(self):
        """Tell the portal our app id (non-sandboxed apps). Errors are fine."""
        def done(*_):
            self._create_session()

        try:
            self._portal.Register(self.app_id, dbus.Dictionary({}, signature="sv"),
                                  dbus_interface=IFACE_REGISTRY,
                                  reply_handler=done, error_handler=lambda e: (
                                      log.debug("host Registry.Register unsupported: %s", e),
                                      done()))
        except Exception as e:  # noqa: BLE001
            log.debug("host Registry.Register unavailable: %s", e)
            self._create_session()

    def _create_session(self):
        # Subscribe to Activated before anything can fire it.
        self._matches.append(self.bus.add_signal_receiver(
            self._on_activated_signal, signal_name="Activated",
            dbus_interface=IFACE_SHORTCUTS, bus_name=PORTAL_BUS, path=PORTAL_PATH))
        self._call("CreateSession",
                   [{"session_handle_token": dbus.String(_token("session"))}],
                   self._on_session)

    def _on_session(self, code, results):
        if code != 0:
            self._fail(f"CreateSession refused (response {code})")
            return
        self.session_handle = str(results.get("session_handle", ""))
        if not self.session_handle:
            self._fail("CreateSession returned no session handle")
            return
        log.debug("GlobalShortcuts session %s", self.session_handle)
        self._call("ListShortcuts",
                   [dbus.ObjectPath(self.session_handle), {}], self._on_list)

    def _on_list(self, code, results):
        bound = {}
        if code == 0:
            for sid, props in results.get("shortcuts", []):
                bound[str(sid)] = dict(props)
        # Bind every time, even if the desktop remembers the shortcut from an
        # earlier run: KDE only activates shortcuts bound in the *current*
        # session, so skipping this leaves the key dead after a restart.
        # Re-binding a known shortcut keeps the user's chosen key and shows no dialog.
        if self.shortcut_id in bound:
            log.debug("shortcut already known: %s", bound[self.shortcut_id])
        shortcut = dbus.Struct(
            (dbus.String(self.shortcut_id), dbus.Dictionary({
                "description": dbus.String(self.description),
                "preferred_trigger": dbus.String(self.trigger),
            }, signature="sv")), signature=None)
        self._call("BindShortcuts",
                   [dbus.ObjectPath(self.session_handle),
                    dbus.Array([shortcut], signature="(sa{sv})"),
                    dbus.String(""), {}],
                   self._on_bind)

    def _on_bind(self, code, results):
        if code == 1:
            self._fail("the shortcut dialog was cancelled")
            return
        if code != 0:
            self._fail(f"BindShortcuts refused (response {code})")
            return
        for sid, props in results.get("shortcuts", []):
            if str(sid) == self.shortcut_id:
                self._set_active(dict(props))
                return
        self._fail("BindShortcuts did not bind our shortcut")

    def _set_active(self, props: dict):
        self.active = True
        trig = str(props.get("trigger_description", "") or self.trigger)
        log.info("Global hotkey ready: %s (%s)", trig or "unassigned", self.description)

    def _on_activated_signal(self, session_handle, shortcut_id, timestamp=0, options=None):
        log.debug("Activated %s on %s (ours: %s)", shortcut_id, session_handle, self.session_handle)
        if self._closed or str(session_handle) != self.session_handle:
            return
        if str(shortcut_id) != self.shortcut_id:
            return
        log.info("hotkey pressed")
        try:
            self.on_activated()
        except Exception:  # noqa: BLE001
            log.exception("hotkey callback failed")

    # ---------------------------------------------------------------- close
    def close(self) -> None:
        self._closed = True
        for m in self._matches:
            try:
                m.remove()
            except Exception:  # noqa: BLE001
                pass
        self._matches.clear()
        if self.session_handle:
            try:
                sess = self.bus.get_object(PORTAL_BUS, self.session_handle, introspect=False,
                                           follow_name_owner_changes=True)
                sess.Close(dbus_interface=IFACE_SESSION,
                           reply_handler=lambda: None, error_handler=lambda e: None)
            except Exception:  # noqa: BLE001
                pass
            self.session_handle = None
        self.active = False
