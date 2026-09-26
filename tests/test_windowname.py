"""Window titles from ScreenCast restore data: python3 -m unittest tests.test_windowname

Fixtures are synthesised in each backend's format; no test touches the real bus.
"""

from __future__ import annotations

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import windowname as wn  # noqa: E402

# --- QDataStream writer, just enough for xdg-desktop-portal-kde's payload -------

QT6_USER, QT5_USER = 65536, 1024
Q_VARIANT_LIST, Q_STRING, Q_STRING_LIST, Q_RECT = 9, 10, 11, 19


def u32(n: int) -> bytes:
    return struct.pack(">I", n)


def qstr(s: str | None) -> bytes:
    if s is None:
        return u32(0xFFFFFFFF)
    raw = s.encode("utf-16-be")
    return u32(len(raw)) + raw


def variant(type_id: int, body: bytes) -> bytes:
    return u32(type_id) + b"\0" + body


def windows_variant(windows, user_type=QT6_USER) -> bytes:
    name = b"QList<WindowRestoreInfo>\0"
    body = u32(len(name)) + name + u32(len(windows))
    for app_id, title in windows:
        body += qstr(app_id) + qstr(title)
    return variant(user_type, body)


def kde_payload(windows_value: bytes, outputs=()) -> bytes:
    """A QVariantMap laid out like Plasma 6's: outputs, region, windows (sorted keys)."""
    out_list = u32(len(outputs)) + b"".join(variant(Q_STRING, qstr(o)) for o in outputs)
    entries = [
        (qstr("outputs"), variant(Q_VARIANT_LIST, out_list)),
        (qstr("region"), variant(Q_RECT, struct.pack(">iiii", 0, 0, -1, -1))),
        (qstr("windows"), windows_value),
    ]
    return u32(len(entries)) + b"".join(k + v for k, v in entries)


def kde(windows, **kw) -> bytes:
    return kde_payload(windows_variant(windows, **kw))


class KdeTest(unittest.TestCase):
    def test_title(self):
        self.assertEqual(wn.parse_kde(kde([("org.kde.kate", "notes.txt - Kate")])), "notes.txt - Kate")

    def test_qt5_user_type_id(self):
        self.assertEqual(wn.parse_kde(kde([("kate", "Session")], user_type=QT5_USER)), "Session")

    def test_multiple_windows_first_wins(self):
        self.assertEqual(wn.parse_kde(kde([("a.b.game", "Some Game"), ("org.kde.kate", "Other")])), "Some Game")

    def test_null_app_id(self):
        self.assertEqual(wn.parse_kde(kde([(None, "Only A Title")])), "Only A Title")

    def test_null_or_blank_title_falls_back_to_app_name(self):
        self.assertEqual(wn.parse_kde(kde([("org.kde.dolphin", None)])), "Dolphin")
        self.assertEqual(wn.parse_kde(kde([("org.kde.dolphin", " \t ")])), "Dolphin")

    def test_ugly_app_id_without_title_is_none(self):
        self.assertIsNone(wn.parse_kde(kde([("steam_app_1245620", None)])))
        self.assertIsNone(wn.parse_kde(kde([(None, None)])))

    def test_non_ascii_title(self):
        self.assertEqual(wn.parse_kde(kde([("steam_app_1", "GAME RING™ — 日本語 🎮")])), "GAME RING™ — 日本語 🎮")

    def test_no_windows(self):
        self.assertIsNone(wn.parse_kde(kde([])))
        # A monitor token: outputs only, empty window list.
        self.assertIsNone(wn.parse_kde(kde_payload(windows_variant([]), outputs=["DP-1"])))

    def test_pre_plasma_6_3_uuid_list_is_none(self):
        uuids = u32(1) + qstr("{0b8f5c7e-1111-2222-3333-444455556666}")
        self.assertIsNone(wn.parse_kde(kde_payload(variant(Q_STRING_LIST, uuids))))

    def test_marker_without_user_type_is_ignored(self):
        data = kde([("x", "Title")])
        at = data.index(b"QList<") - 4
        broken = data[: at - 5] + u32(Q_STRING) + data[at - 1:]
        self.assertIsNone(wn.parse_kde(broken))

    def test_truncated_data_is_none(self):
        data = kde([("org.kde.kate", "notes.txt - Kate")])
        cut_from = data.index(b"QList<") + len("QList<WindowRestoreInfo>") + 1
        for n in range(cut_from, len(data)):
            self.assertIsNone(wn.parse_kde(data[:n]), n)

    def test_garbage_is_none(self):
        for junk in (b"", b"\xff" * 64, bytes(range(256)), "not bytes", None, 42, [300, 1]):
            self.assertIsNone(wn.parse_kde(junk), junk)

    def test_odd_length_string_is_none(self):
        data = kde([("ab", "Title")])
        at = data.index(qstr("ab"))
        self.assertIsNone(wn.parse_kde(data[:at] + u32(3) + data[at + 4:]))

    def test_accepts_dbus_style_byte_sequence(self):
        data = kde([("org.kde.kate", "notes.txt - Kate")])
        self.assertEqual(wn.parse_kde([int(b) for b in data]), "notes.txt - Kate")


class GnomeTest(unittest.TestCase):
    def test_window_stream(self):
        data = (1700000000000000, 1700000001000000, [(0, 2, ("org.gnome.TextEditor.desktop", "notes.txt"))])
        self.assertEqual(wn.parse_gnome(data), "notes.txt")

    def test_skips_monitors_and_uses_first_window(self):
        data = (1, 2, [(0, 1, "DP-1 match string"), (1, 2, ("a.b.c", "First")), (2, 2, ("a.b.d", "Second"))])
        self.assertEqual(wn.parse_gnome(data), "First")

    def test_app_id_fallback(self):
        self.assertEqual(wn.parse_gnome((1, 2, [(0, 2, ("org.gnome.Nautilus.desktop", ""))])), "Nautilus")
        self.assertIsNone(wn.parse_gnome((1, 2, [(0, 2, ("window:42", ""))])))

    def test_monitor_only_and_garbage(self):
        self.assertIsNone(wn.parse_gnome((1, 2, [(0, 1, "DP-1")])))
        self.assertIsNone(wn.parse_gnome((1, 2, [(0, 2, "xy")])))
        for junk in (None, b"\0\1", (1, 2), (1, 2, [(0,)]), {"a": 1}):
            self.assertIsNone(wn.parse_gnome(junk), junk)


class HyprlandTest(unittest.TestCase):
    def test_window_class(self):
        self.assertEqual(wn.parse_hyprland({"windowClass": "firefox", "windowHandle": 1}), "Firefox")
        self.assertIsNone(wn.parse_hyprland({"windowClass": "steam_app_1245620"}))
        self.assertIsNone(wn.parse_hyprland({"output": "DP-1"}))
        self.assertIsNone(wn.parse_hyprland(("todo", 1, "DP-1", True, 0)))  # version 2 struct


class DispatchTest(unittest.TestCase):
    def test_backends(self):
        self.assertEqual(wn.parse_restore_data("KDE", 1, kde([("a", "K Title")])), "K Title")
        self.assertEqual(wn.parse_restore_data("GNOME", 1, (1, 2, [(0, 2, ("a", "G Title"))])), "G Title")
        self.assertEqual(wn.parse_restore_data("hyprland", 3, {"windowClass": "org.kde.kate"}), "Kate")
        self.assertIsNone(wn.parse_restore_data("wlroots", 1, {"output_name": "DP-1"}))
        self.assertIsNone(wn.parse_restore_data("COSMIC", 1, ([], ["toplevel-id"])))
        self.assertIsNone(wn.parse_restore_data("KDE", 1, None))


class CleanTest(unittest.TestCase):
    def test_whitespace_and_controls(self):
        self.assertEqual(wn.clean_title("  a\t\tb\n c  "), "a b c")
        self.assertEqual(wn.clean_title("a\x00b\x1b[31mc‮​d"), "ab[31mcd")
        self.assertEqual(wn.clean_title("👨‍👩"), "👨‍👩")  # ZWJ kept
        self.assertIsNone(wn.clean_title("\x00\n\t "))
        self.assertIsNone(wn.clean_title(None))

    def test_length_cap(self):
        out = wn.clean_title("x" * 500)
        self.assertEqual(len(out), wn.MAX_LEN)
        self.assertTrue(out.endswith("…"))

    def test_app_name(self):
        cases = {
            "org.kde.dolphin": "Dolphin",
            "org.gnome.Nautilus.desktop": "Nautilus",
            "com.github.tchx84.Flatseal": "Flatseal",
            "google-chrome": "Google Chrome",
            "cursor": "Cursor",
            "Alacritty": "Alacritty",
        }
        for app_id, name in cases.items():
            self.assertEqual(wn.app_name(app_id), name, app_id)
        for ugly in ("steam_app_1245620", "eldenring.exe", "window:42", "gamescope", "hl2_linux",
                     "org.example.1234567", "x", "", None, "  "):
            self.assertIsNone(wn.app_name(ugly), ugly)


class FakeStore:
    def __init__(self, entries):
        self.entries, self.calls = entries, []

    def Lookup(self, table, token, dbus_interface=None, timeout=None):  # noqa: N802 - D-Bus name
        self.calls.append((table, token, dbus_interface))
        entry = self.entries[token]
        if isinstance(entry, Exception):
            raise entry
        return {"io.github.mehulchachada.Momento": ["yes"]}, entry


class FakeBus:
    def __init__(self, entries):
        self.store = FakeStore(entries)

    def get_object(self, name, path, introspect=True):
        assert (name, path) == (wn.STORE_BUS, wn.STORE_PATH)
        return self.store


class TitleForTokenTest(unittest.TestCase):
    def test_lookup(self):
        bus = FakeBus({"tok": ("KDE", 1, kde([("org.kde.kate", "notes.txt - Kate")]))})
        self.assertEqual(wn.title_for_token("tok", bus=bus), "notes.txt - Kate")
        self.assertEqual(bus.store.calls, [("screencast", "tok", wn.STORE_IFACE)])

    def test_failures_are_none(self):
        bus = FakeBus({"err": RuntimeError("NotFound"), "short": ("KDE", 1), "bad": ("KDE", "x", b"")})
        for token in ("missing", "err", "short", "bad", ""):
            self.assertIsNone(wn.title_for_token(token, bus=bus), token)
        self.assertIsNone(wn.title_for_token("tok", bus=object()))

    def test_dbus_types(self):
        try:
            import dbus
        except ImportError:
            self.skipTest("dbus-python not installed")
        payload = dbus.Array([dbus.Byte(b) for b in kde([("org.kde.kate", "notes.txt - Kate")])], signature="y")
        data = dbus.Struct((dbus.String("KDE"), dbus.UInt32(1), payload), variant_level=1)
        self.assertEqual(wn.title_for_token("tok", bus=FakeBus({"tok": data})), "notes.txt - Kate")
        gnome = dbus.Struct((dbus.Int64(1), dbus.Int64(2), dbus.Array(
            [dbus.Struct((dbus.UInt32(0), dbus.UInt32(2),
                          dbus.Struct((dbus.String("a.b.c"), dbus.String("G Title")), variant_level=1)))],
            signature="(uuv)")), variant_level=1)
        self.assertEqual(wn.parse_restore_data("GNOME", 1, gnome), "G Title")


if __name__ == "__main__":
    unittest.main()
