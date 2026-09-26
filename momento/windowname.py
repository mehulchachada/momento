"""The human title of the window picked in the ScreenCast portal's window picker.

The portal's streams carry no title, but with persist_mode=2 xdg-desktop-portal
keeps the backend's restore data in the permission store (table "screencast",
keyed by our restore token), and most backends put the window's title and/or
app id there so they can find the window again. The data is always
``(s backend, u version, v payload)``; the payload is backend private:

* KDE (Plasma 6.3+): a QDataStream-serialised QVariantMap whose "windows" entry
  is a ``QList<WindowRestoreInfo>``, each ``(QString appId, QString title)``.
  Older Plasma stored window UUIDs only.
* GNOME (also niri, which uses the GNOME portal): ``(x created, x last_used,
  a(uuv) streams)``; a window stream's ``v`` is ``(s app_id, s title)``.
* hyprland 3: ``a{sv}`` with "windowClass" only, no title.
* wlroots and COSMIC keep no window information (an output name, an opaque
  toplevel id), so they give None.

Versions are not checked strictly: each parser validates the shape it needs,
so a format bump that keeps the window entry still works and anything else
gives None. Everything here is best effort and never raises; None makes the
bar say "Recording Window".
"""

from __future__ import annotations

import functools
import logging
import re
import struct
import unicodedata

log = logging.getLogger("momento.windowname")

STORE_BUS = "org.freedesktop.impl.portal.PermissionStore"
STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
STORE_IFACE = "org.freedesktop.impl.portal.PermissionStore"
TABLE = "screencast"
SOURCE_WINDOW = 2
MAX_LEN = 80

# How Qt writes the QVariant holding the window list: quint32 type id
# (QMetaType::User: 1024 in Qt 5, 65536 in Qt 6), qint8 is-null flag, then the
# type name as a C string (quint32 length including the NUL).
_KDE_TYPE = b"QList<WindowRestoreInfo>\0"
_KDE_MARKER = struct.pack(">I", len(_KDE_TYPE)) + _KDE_TYPE
_QT_USER_TYPES = (1024, 65536)
_QT_NULL_STRING = 0xFFFFFFFF

# App ids we would rather not show: Steam's "steam_app_1245620", "window:12"
# (GNOME, no .desktop match), "eldenring.exe", wrappers that say nothing.
_APP_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*")
_GENERIC_APPS = {"gamescope", "wine", "wine64", "java", "python", "python3", "electron", "xwayland"}


def title_for_token(token: str, bus=None) -> str | None:
    """Best-effort human title of the window stored under this ScreenCast restore token, or None."""
    if not token:
        return None
    try:
        if bus is None:
            import dbus

            bus = dbus.SessionBus()
        store = bus.get_object(STORE_BUS, STORE_PATH, introspect=False)
        _permissions, data = store.Lookup(TABLE, token, dbus_interface=STORE_IFACE, timeout=2)
        backend, version, payload = data
        title = parse_restore_data(str(backend), int(version), payload)
        log.debug("window title from %s %s restore data: %r", backend, version, title)
        return title
    except Exception as e:  # noqa: BLE001 - a missing title is never worth an error
        log.debug("no window title for restore token: %s", e)
        return None


def parse_restore_data(backend: str, version: int, data) -> str | None:
    """Title (or a readable app name) from one backend's restore payload, or None.

    ``version`` is informational only; see the module docstring.
    """
    parser = {"KDE": parse_kde, "GNOME": parse_gnome, "hyprland": parse_hyprland}.get(backend)
    return parser(data) if parser else None


def _never_raises(parse):
    """Garbage in, None out: restore data is another program's private format."""
    @functools.wraps(parse)
    def wrapper(data):
        try:
            return parse(data)
        except Exception as e:  # noqa: BLE001
            log.debug("%s: unreadable restore data: %r", parse.__name__, e)
            return None
    return wrapper


@_never_raises
def parse_kde(data) -> str | None:
    """First window of xdg-desktop-portal-kde's QDataStream payload (bytes or D-Bus byte array).

    Rather than walk every QVariant in the map (whose value types vary between
    Plasma releases), find the window list by its type name and read from there.
    """
    data = bytes(bytearray(int(b) for b in data))
    at = data.find(_KDE_MARKER)
    while at >= 5:
        (type_id,) = struct.unpack_from(">I", data, at - 5)
        if type_id in _QT_USER_TYPES and data[at - 1] == 0:
            pos = at + len(_KDE_MARKER)
            (count,) = struct.unpack_from(">I", data, pos)
            if count < 1:
                return None
            app_id, pos = _qstring(data, pos + 4)
            title, pos = _qstring(data, pos)
            return pick(app_id, title)
        at = data.find(_KDE_MARKER, at + 1)
    return None


@_never_raises
def parse_gnome(data) -> str | None:
    """First window stream of xdg-desktop-portal-gnome's ``(xx a(uuv))`` payload."""
    _created, _last_used, streams = data
    for _id, source_type, info in streams:
        if int(source_type) == SOURCE_WINDOW and not isinstance(info, str) and len(info) == 2:
            return pick(str(info[0]), str(info[1]))
    return None


@_never_raises
def parse_hyprland(data) -> str | None:
    """xdg-desktop-portal-hyprland's ``a{sv}`` payload (version 3): a window class, no title."""
    window_class = data.get("windowClass")
    return app_name(str(window_class)) if window_class else None


def pick(app_id: str | None, title: str | None) -> str | None:
    """The cleaned title, else a readable app name, else None."""
    return clean_title(title) or app_name(app_id)


def clean_title(text: str | None) -> str | None:
    """Strip, collapse whitespace, drop control/format characters, cap at MAX_LEN."""
    if not text:
        return None
    text = " ".join(str(text).split())
    # Keep ZWJ so emoji sequences survive; drop bidi overrides and other invisibles.
    text = "".join(c for c in text if c == "‍" or unicodedata.category(c)[0] != "C").strip()
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1].rstrip() + "…"
    return text or None


def app_name(app_id: str | None) -> str | None:
    """'org.kde.dolphin' -> 'Dolphin', 'google-chrome' -> 'Google Chrome'; None for ids nobody wants to read."""
    if not app_id:
        return None
    name = app_id.strip().removesuffix(".desktop")
    if not _APP_ID.fullmatch(name):
        return None
    if "." in name:
        if name.count(".") < 2:  # not reverse-DNS: "eldenring.exe", "run.sh"
            return None
        name = name.rsplit(".", 1)[1]
    if name.lower() in _GENERIC_APPS or re.search(r"\d{3,}", name) or len(name) < 2:
        return None
    name = name.replace("-", " ")
    return name.title() if name.islower() else name


def _qstring(data: bytes, pos: int) -> tuple[str | None, int]:
    """Read a QDataStream QString: quint32 byte length (0xFFFFFFFF = null), UTF-16BE."""
    (size,) = struct.unpack_from(">I", data, pos)
    pos += 4
    if size == _QT_NULL_STRING:
        return None, pos
    if size % 2 or pos + size > len(data):
        raise ValueError(f"bad QString length {size} at {pos - 4}")
    return data[pos:pos + size].decode("utf-16-be", errors="replace"), pos + size
