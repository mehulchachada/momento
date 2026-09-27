"""Game controllers: find them, turn their input into bar actions, detect the
"open the bar" chord, and hold them exclusively while the bar is open.

Everything here reads evdev nodes (``/dev/input/event*``) that the logged-in
user can already open (systemd-logind's ``uaccess`` ACL); nothing needs root.
python-evdev is imported lazily: without it this module still imports and
:func:`available` returns False.

Design
------
:class:`Gamepads` is a loop-agnostic core. It owns the open devices and a
hotplug watch and exposes

* ``fds()``      - file descriptors to watch for input,
* ``process(fd)``- read and handle what is waiting on one of them,
* ``tick()``     - run timers (auto-repeat, chord hold, grab wait, watchdog),
* ``next_timeout()`` - seconds until ``tick()`` is next needed (or None).

:meth:`Gamepads.attach_glib` (daemon) and :meth:`Gamepads.attach_qt` (bar)
are thin adapters that wire those to a GLib main loop or a Qt event loop.
The logic below the device layer takes plain ``(type, code, value)`` events
and an injectable clock, so it is tested without hardware (:class:`FakeDevice`).

Actions (brand independent, positional)
---------------------------------------
============  ==========================================================
up/down/...   D-pad (``ABS_HAT0X/Y``, ``BTN_DPAD_*``, xpad's
              ``BTN_TRIGGER_HAPPY1-4`` on pads with neither) and the left stick
              past ``deadzone``; auto-repeat after 350 ms, then every 90 ms
accept        bottom face button (``BTN_SOUTH``: A / Cross)
back          right face button (``BTN_EAST``: B / Circle)
settings      top face button (Y / Triangle)
pause         left face button (X / Square)
prev_section  left bumper (``BTN_TL``);  next_section: right bumper
left_trigger  left trigger (``BTN_TL2`` press, or ``ABS_Z`` past 0.6 of its
              travel, re-armed below 0.3); right_trigger: ``BTN_TR2``/``ABS_RZ``.
              One action per pull, no auto-repeat: a pad that reports both the
              button and the axis (DualShock/DualSense) fires once. The axis
              only counts when its range is unsigned (0..max) and it rested
              below 0.3 when the pad was opened (a centred axis is a stick)
============  ==========================================================

Raw buttons (``on_button(name, pressed)``; also the names a chord uses):
south east north west tl tr tl2 tr2 select start mode thumbl thumbr
dpad_up dpad_down dpad_left dpad_right paddle1..paddle4 extra1..extra4.
Chord-only groups: ``left_paddle`` (paddle3 or paddle4), ``right_paddle``
(paddle1 or paddle2). Aliases: view=select, menu=start, guide/home/ps/xbox=mode,
l3=thumbl, r3=thumbr, lb=tl, rb=tr, lt=tl2, rt=tr2, up/down/left/right=dpad_*,
and the pads' own names by position: a/b/x/y (Xbox), cross/circle/square/triangle,
l1/r1/l2/r2, share/create/options, minus/plus.
In a chord, ``dpad_*`` also matches the hat (``ABS_HAT0X/Y``), which is how
most pads report their D-pad; others send ``BTN_DPAD_*``.

Layout notes
------------
* **North/west.** The kernel's gamepad spec says ``BTN_NORTH`` (0x133) is
  the top button, and hid-playstation / hid-nintendo follow it. xpad (Xbox
  pads, and InputPlumber's / Steam's virtual Xbox pads) reports X (left) as
  0x133 and Y (top) as 0x134, the other way round. Pads driven by xpad or
  hid-steam, or with a Microsoft/Valve vendor id, get the "xbox" layout, so
  "settings" is always the top button and "pause" the left one.
* **Nintendo.** hid-nintendo reports buttons by position, so "accept" is the
  bottom button even though Nintendo labels it B (and "back" is the right
  one, labelled A). There is no automatic A/B swap for now.
* **Symbols.** The bar's hints name a pad's own buttons, by position: ✕ ○ □ △,
  L1/R1, L2/R2 on a PlayStation pad (hid-playstation / hid-sony, or Sony's
  vendor id), B A Y X, L/R, ZL/ZR on a Nintendo one (hid-nintendo, or
  Nintendo's vendor id), Xbox letters on everything else (:func:`symbols_for`,
  :data:`SYMBOLS`). The pad *in use* (:meth:`Gamepads.active_pad`) is the last
  one that sent a press, else the only one connected; ``on_active`` fires when
  it changes. A press from another pad within ``CHORD_DEDUP_S`` of the current
  one's doesn't switch it, since Steam's virtual Xbox pad mirrors the real one.
* **Paddles.** xpad reports the Elite paddles as ``BTN_TRIGGER_HAPPY5-8``
  (P1 upper right, P2 lower right, P3 upper left, P4 lower left); InputPlumber's
  virtual Elite 2 pad uses the same codes for handheld back buttons.
  ``BTN_TRIGGER_HAPPY1-4`` are xpad's D-pad-as-buttons, not paddles.

Exclusive grab
--------------
While the bar is open, :meth:`Gamepads.grab` takes every pad with
``EVIOCGRAB`` so the game doesn't see the presses. A pad with a button held
(for example the chord that opened the bar) is grabbed once everything on it
is released, or after ``grab_wait_s`` at most, so the game never keeps a
"stuck" button. A pad that can't be grabbed (someone else holds it) stays
shared; that is logged once. :meth:`ungrab` releases everything; it is also
called on :meth:`close`, at interpreter exit, when a callback raises, and by a
watchdog when nothing happened for ``watchdog_s`` (60 s). The kernel drops a
grab by itself when the process exits.

Shortcut with a D-pad direction (the default, PS/Xbox/Home + D-pad Down)
-------------------------------------------------------------------------
The chord's other buttons are its *modifier* (``mode``). The daemon's hub
(``navigate=False``) reads key events and the hat only (EVIOCSMASK, the hat
limited per code), so stick movement never wakes it; a D-pad press does, which
is what lets it tell the order of PS and Down.

*Order.* The chord opens the bar only when the modifier came first: the D-pad
direction pressed while it is held, or at most ``ORDER_TOLERANCE_S`` (30 ms)
before it (pressed "together"). A direction held clearly longer before the
modifier was a game press; the chord is then ignored until it is let go.

*Grab.* With ``chord_grab`` (the daemon, when ``[controller] exclusive``) the
hub takes the pad with ``EVIOCGRAB`` the moment it handles the modifier's press
(synchronously, before the rest of that read), so a D-pad press that comes
after it never reaches the game. Events the kernel queued before the grab (the
same read as the modifier, or an earlier kernel timestamp) already went to every
other reader: such a D-pad press "leaked", and the grab is then let go at once
so the game also sees its release (no stuck Down). The same goes for a direction
already held when the modifier goes down: no grab. Otherwise the grab is kept
until the modifier *and* the chord's direction are released (the game never
sees a release without its press), ``chord_grab_s`` (2 s) at most (checked in
``tick()`` and by the watchdog thread), on any error, and on close. Every use is
logged once at INFO (order, timing, whether the grab came first, how long it was
held), and a pad that can't be grabbed is logged once with its path and whether
it is a virtual pad (InputPlumber, Steam Input, uinput/uhid). An evdev grab
only hides the event node: a program that reads the pad some other way (Steam
Input through hidraw) still sees everything. A pad someone else holds stays shared. The
open bar's own grab takes over: a grab that fails with EBUSY (the daemon still
holds the pad) is retried every 50 ms for ``grab_busy_s`` before the pad counts
as shared, and a waiting pad re-reads its state from the kernel, since another
program's grab hides events from us. In a navigating hub the D-pad does nothing
while the modifier is held. Chords without a D-pad direction are watched
without grabbing, so their own presses reach the game.

Under the test sandbox (``MOMENTO_TEST_SANDBOX``, see tests/_sandbox.py) the
real device layer is off: :func:`open_device` and :func:`probe` find nothing and
:meth:`Gamepads.start` with the default opener does nothing. Tests use
:class:`FakeDevice`.
"""

from __future__ import annotations

import atexit
import errno
import logging
import os
import struct
import sys
import threading
import time
import weakref
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# --------------------------------------------------------------- event codes
# linux/input-event-codes.h (kept here so the logic needs no python-evdev)
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT, SYN_DROPPED = 0, 3

BTN_SOUTH, BTN_EAST, BTN_C = 0x130, 0x131, 0x132
BTN_X, BTN_Y, BTN_Z = 0x133, 0x134, 0x135      # BTN_NORTH == BTN_X, BTN_WEST == BTN_Y
BTN_NORTH, BTN_WEST = BTN_X, BTN_Y
BTN_GAMEPAD = BTN_SOUTH
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE = 0x13a, 0x13b, 0x13c
BTN_THUMBL, BTN_THUMBR = 0x13d, 0x13e
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
BTN_TRIGGER_HAPPY1 = 0x2c0          # ..HAPPY8 = 0x2c7

ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

ACTIONS = ("up", "down", "left", "right", "accept", "back", "settings", "pause",
           "prev_section", "next_section", "left_trigger", "right_trigger")
DIRECTIONS = ("up", "down", "left", "right")

BUTTON_ACTIONS = {"south": "accept", "east": "back", "north": "settings", "west": "pause",
                  "tl": "prev_section", "tr": "next_section"}
DPAD_BUTTONS = {"dpad_up": "up", "dpad_down": "down", "dpad_left": "left", "dpad_right": "right"}
# (action, digital button, analog axis), indexed like _Pad.trig_latched
TRIGGERS = (("left_trigger", BTN_TL2, ABS_Z), ("right_trigger", BTN_TR2, ABS_RZ))

_COMMON = {
    BTN_SOUTH: "south", BTN_EAST: "east", BTN_TL: "tl", BTN_TR: "tr",
    BTN_TL2: "tl2", BTN_TR2: "tr2", BTN_SELECT: "select", BTN_START: "start",
    BTN_MODE: "mode", BTN_THUMBL: "thumbl", BTN_THUMBR: "thumbr",
    BTN_DPAD_UP: "dpad_up", BTN_DPAD_DOWN: "dpad_down",
    BTN_DPAD_LEFT: "dpad_left", BTN_DPAD_RIGHT: "dpad_right",
    **{BTN_TRIGGER_HAPPY1 + 4 + i: f"paddle{i + 1}" for i in range(4)},
}
# xpad's D-pad-as-buttons order (MAP_DPAD_TO_BUTTONS): left, right, up, down
_HAPPY_DPAD = {BTN_TRIGGER_HAPPY1: "dpad_left", BTN_TRIGGER_HAPPY1 + 1: "dpad_right",
               BTN_TRIGGER_HAPPY1 + 2: "dpad_up", BTN_TRIGGER_HAPPY1 + 3: "dpad_down"}
_HAPPY_EXTRA = {BTN_TRIGGER_HAPPY1 + i: f"extra{i + 1}" for i in range(4)}

BUTTON_NAMES = ("south", "east", "north", "west", "tl", "tr", "tl2", "tr2", "select", "start",
                "mode", "thumbl", "thumbr", "dpad_up", "dpad_down", "dpad_left", "dpad_right",
                "paddle1", "paddle2", "paddle3", "paddle4", "extra1", "extra2", "extra3", "extra4")
GROUPS = {"left_paddle": ("paddle3", "paddle4"), "right_paddle": ("paddle1", "paddle2")}
ALIASES = {"view": "select", "menu": "start", "guide": "mode", "home": "mode", "ps": "mode",
           "xbox": "mode", "l3": "thumbl", "r3": "thumbr", "lb": "tl", "rb": "tr", "lt": "tl2", "rt": "tr2",
           "up": "dpad_up", "down": "dpad_down", "left": "dpad_left", "right": "dpad_right",
           # the names on the pads (by position): Xbox letters, PlayStation, Nintendo
           "a": "south", "b": "east", "x": "west", "y": "north",
           "cross": "south", "circle": "east", "square": "west", "triangle": "north",
           "l1": "tl", "r1": "tr", "l2": "tl2", "r2": "tr2", "share": "select", "create": "select",
           "options": "start", "minus": "select", "plus": "start"}
DPAD_NAMES = ("dpad_up", "dpad_down", "dpad_left", "dpad_right")

DEFAULT_CHORD = ("mode", "dpad_down")
DEFAULT_HOLD_MS = 0        # open on a tap, like [controller] hold_ms
# (key, label, buttons): every shortcut known by name. Settings (config, `momento set
# controller`, configure) take all of them; the bar offers only CHORD_OFFERED (plus Off),
# and shows any other saved shortcut as an extra choice with its buttons.
# Rule for new shortcuts (and the custom bind to come): exactly CHORD_SIZE buttons
# pressed together (check_chord_size). `momento set controller` enforces it; a
# hand-edited open_chord of another size keeps working, with a warning (config.controller).
CHORD_SIZE = 2
CHORD_PRESETS = (
    ("ps_down", "PS / Xbox + Down", ("mode", "dpad_down")),
    ("view_menu", "View + Menu", ("select", "start")),
    ("left_paddle", "Left paddle", ("left_paddle",)),
    ("right_paddle", "Right paddle", ("right_paddle",)),
    ("l3_r3", "L3 + R3", ("thumbl", "thumbr")),
)

# The pad's own names for its buttons, by position (the hints; see symbols_for). The
# PlayStation face symbols are drawn as vector glyphs by the bar, never as font text.
SYMBOLS = {
    "xbox": {"south": "A", "east": "B", "west": "X", "north": "Y", "tl": "LB", "tr": "RB",
             "tl2": "LT", "tr2": "RT", "select": "View", "start": "Menu", "mode": "Xbox",
             "thumbl": "L3", "thumbr": "R3"},
    "playstation": {"south": "✕", "east": "○", "west": "□", "north": "△", "tl": "L1", "tr": "R1",
                    "tl2": "L2", "tr2": "R2", "select": "Create", "start": "Options", "mode": "PS",
                    "thumbl": "L3", "thumbr": "R3"},
    "nintendo": {"south": "B", "east": "A", "west": "Y", "north": "X", "tl": "L", "tr": "R",
                 "tl2": "ZL", "tr2": "ZR", "select": "Minus", "start": "Plus", "mode": "Home",
                 "thumbl": "L3", "thumbr": "R3"},
}
CHORD_OFFERED = ("ps_down",)
PS_FACE_WORDS = {"✕": "Cross", "○": "Circle", "□": "Square", "△": "Triangle"}

DEADZONE = 0.5            # stick deflection (0..1) that counts as a direction
RELEASE_FRACTION = 0.7    # ...and it lets go below DEADZONE * this (hysteresis)
TRIGGER_PRESS = 0.6       # analog trigger travel (0..1) that counts as a pull
TRIGGER_RELEASE = 0.3     # ...re-armed once it is back below this
REPEAT_DELAY_MS = 350
REPEAT_INTERVAL_MS = 90
WATCHDOG_S = 60.0
GRAB_WAIT_S = 1.0
GRAB_BUSY_S = 2.5         # EBUSY (the daemon's shortcut hold) is retried this long
GRAB_POLL_S = 0.05        # ...this often, re-reading the pad's state from the kernel
CHORD_GRAB_S = 2.0        # the shortcut's modifier hold is let go after this at most
ORDER_TOLERANCE_S = 0.030 # the chord's D-pad this much before its modifier still counts
RESCAN_S = 2.0
ACTION_DEDUP_S = 0.05     # same action from another pad this soon = the same press
CHORD_DEDUP_S = 0.25      # (Steam's virtual pad mirrors the physical one)

INPUT_DIR = "/dev/input"


# ------------------------------------------------------------------ helpers
_evdev = None
_evdev_checked = False


def _import_evdev():
    global _evdev, _evdev_checked
    if not _evdev_checked:
        _evdev_checked = True
        try:
            import evdev  # noqa: F401
            _evdev = evdev
        except Exception:  # ImportError, or a broken build
            _evdev = None
    return _evdev


def available() -> bool:
    """Is python-evdev importable (needed for real devices)?"""
    return _import_evdev() is not None


_warned: set = set()


def _warn_once(key, msg, *args, level=logging.WARNING) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.log(level, msg, *args)


def normalize_chord(buttons) -> tuple[str, ...]:
    """``["select", "start"]`` or ``"view+menu"`` -> canonical names; ValueError if unknown."""
    if isinstance(buttons, str):
        buttons = [b for b in buttons.replace(",", "+").split("+")]
    out = []
    for b in buttons:
        name = str(b).strip().lower().replace("-", "_").replace(" ", "_")
        name = name.replace("d_pad_", "dpad_")      # "D-pad Down" -> dpad_down
        name = ALIASES.get(name, name)
        if name not in BUTTON_NAMES and name not in GROUPS:
            raise ValueError(f"unknown controller button {b!r}; use one of: "
                             + ", ".join(BUTTON_NAMES + tuple(GROUPS)))
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("a controller shortcut needs at least one button")
    return tuple(out)


def check_chord_size(buttons) -> tuple[str, ...]:
    """The canonical chord if it is CHORD_SIZE buttons, else ValueError in plain words."""
    names = normalize_chord(buttons)
    if len(names) != CHORD_SIZE:
        raise ValueError(f"a controller shortcut is two buttons pressed together "
                         f"(got {len(names)}: {' + '.join(names)})")
    return names


def button_word(name: str, symbols: str = "xbox") -> str:
    """A button's name in running text, as a ``symbols`` pad labels it ("L1", "Cross",
    "Create", "Down", "Left paddle", "P3")."""
    label = button_symbol(name, symbols)
    if label != name:
        return PS_FACE_WORDS.get(label, label)
    if name in DPAD_NAMES:
        return name[5:].capitalize()
    if name.startswith("paddle"):
        return "P" + name[6:]
    return name.replace("_", " ").replace("extra", "extra ").strip().capitalize()


def chord_label(buttons, symbols: str | None = None) -> str:
    """Human name for a chord: the preset label, else "Select + Start"-style. With
    ``symbols`` (a pad's, see SYMBOLS) a shortcut the bar doesn't offer is named by its
    buttons as that pad labels them: "View + Menu", "Create + Options", "L1 + R1"."""
    names = normalize_chord(buttons)
    for key, label, preset in CHORD_PRESETS:
        if tuple(preset) == names and (symbols is None or key in CHORD_OFFERED):
            return label
    if symbols is not None:
        return " + ".join(button_word(n, symbols) for n in names)
    return " + ".join(n.replace("_", " ").title() for n in names)


def _sysfs_driver(path: str) -> str:
    """Kernel driver behind an event node ("xpad", "playstation", ...), or ""."""
    base = os.path.basename(path)
    for rel in ("device/device/driver", "device/driver"):
        try:
            return os.path.basename(os.readlink(f"/sys/class/input/{base}/{rel}"))
        except OSError:
            continue
    return ""


def device_origin(path: str = "", name: str = "", phys: str = "", vendor: int = 0,
                  product: int = 0) -> str:
    """Where a pad comes from, for the logs: "InputPlumber virtual pad", "Steam Input
    virtual pad", "Bluetooth", "virtual (uhid)", "virtual (uinput)", "physical", or "" (unknown).
    A virtual pad mirrors a physical one that another program reads; a uhid one is
    also readable through hidraw, which an evdev grab doesn't cover."""
    text = f"{name} {phys}".lower()
    if "inputplumber" in text:
        return "InputPlumber virtual pad"
    if (vendor == 0x28de and product == 0x11ff) or "steam virtual" in text:
        return "Steam Input virtual pad"
    base = os.path.basename(path or "")
    if not base.startswith("event"):
        return ""
    try:
        real = os.path.realpath(f"/sys/class/input/{base}/device")
    except OSError:
        return ""
    if not os.path.exists(real):
        return ""
    if "/uhid/" in real:
        # BlueZ creates Bluetooth HID pads through uhid too: HID bus 0005 is a real pad
        try:
            with open(os.path.join(real, "device", "uevent")) as f:
                if any(line.startswith("HID_ID=0005:") for line in f):
                    return "Bluetooth"
        except OSError:
            pass
        return "virtual (uhid)"
    if "/virtual/" in real:
        return "virtual (uinput)"
    return "physical"


def layout_for(vendor: int = 0, driver: str = "", name: str = "") -> str:
    """"xbox" when 0x133/0x134 mean X(left)/Y(top), else "standard" (kernel spec)."""
    if driver in ("xpad", "steam", "hid-steam") or vendor in (0x045e, 0x28de):
        return "xbox"
    n = name.lower()
    if "x-box" in n or "xbox" in n:
        return "xbox"
    return "standard"


def symbols_for(vendor: int = 0, driver: str = "", name: str = "") -> str:
    """Whose button names a pad carries: "playstation", "nintendo" or "xbox" (the rest)."""
    if driver in ("playstation", "hid-playstation", "sony", "hid-sony") or vendor == 0x054c:
        return "playstation"
    if driver in ("nintendo", "hid-nintendo") or vendor == 0x057e:
        return "nintendo"
    return "xbox"


def button_symbol(name: str, symbols: str = "xbox") -> str:
    """``"south"`` -> "A" / "✕" / "B": the label a hint chip shows for a button."""
    return SYMBOLS.get(symbols, SYMBOLS["xbox"]).get(name, name)


def _sysfs_has_key(path: str, code: int) -> bool | None:
    """Pre-filter from sysfs (no open needed); None when it can't tell."""
    base = os.path.basename(path)
    try:
        with open(f"/sys/class/input/{base}/device/capabilities/key") as f:
            words = f.read().split()
    except OSError:
        return None
    return _bitmap_has(words, code)


def _bitmap_has(words: list[str], code: int, bits: int = 64 if sys.maxsize > 2**32 else 32) -> bool:
    """Bit ``code`` of a sysfs capability bitmap ("1f 0 ... 0", most significant word first)."""
    idx = code // bits
    if idx >= len(words):
        return False
    return bool(int(words[-1 - idx], 16) >> (code % bits) & 1)


def _list_event_nodes() -> list[str]:
    try:
        names = os.listdir(INPUT_DIR)
    except OSError:
        return []
    nodes = [os.path.join(INPUT_DIR, n) for n in names if n.startswith("event") and n[5:].isdigit()]
    return sorted(nodes, key=lambda p: int(os.path.basename(p)[5:]))


def list_candidates() -> list[str]:
    """Event nodes that look like gamepads (sysfs says BTN_SOUTH, or unknown)."""
    return [p for p in _list_event_nodes() if _sysfs_has_key(p, BTN_SOUTH) is not False]


def is_gamepad_caps(caps: dict) -> bool:
    keys = set(caps.get(EV_KEY, ()))
    return BTN_SOUTH in keys


# EVIOCSMASK: per-client event filter (Linux 4.4+). The daemon only needs key
# events for the chord, so it masks out the stick/trigger stream and isn't woken
# hundreds of times a second while someone plays. A D-pad chord adds EV_ABS
# while its modifier is held, limited by a per-code mask to the hat, so even
# then the sticks don't wake it (the kernel also drops the empty SYN_REPORTs).
_EVIOCSMASK = (1 << 30) | (16 << 16) | (ord("E") << 8) | 0x93
HAT_ONLY = {EV_ABS: (ABS_HAT0X, ABS_HAT0Y)}
# EVIOCSCLOCKID: event timestamps on CLOCK_MONOTONIC (time.monotonic), so they
# compare with the moment of our grab (was an event queued before it?)
_EVIOCSCLOCKID = (1 << 30) | (4 << 16) | (ord("E") << 8) | 0xa0
_CLOCK_MONOTONIC = 1


def set_monotonic_clock(fd: int) -> bool:
    try:
        import fcntl

        fcntl.ioctl(fd, _EVIOCSCLOCKID, struct.pack("i", _CLOCK_MONOTONIC))
        return True
    except (OSError, ValueError, TypeError):
        return False


def _mask_ioctl(fd: int, type_: int, bits: int) -> None:
    import ctypes
    import fcntl

    buf = ctypes.create_string_buffer(struct.pack("<Q", bits), 8)
    fcntl.ioctl(fd, _EVIOCSMASK, struct.pack("IIQ", type_, 8, ctypes.addressof(buf)))


def set_event_mask(fd: int, types: Iterable[int] | None, codes: dict | None = None) -> bool:
    """Deliver only these event types to this fd (None = everything); ``codes``
    ({type: codes}) narrows a type further (only types with at most 64 codes)."""
    try:
        for t, allowed in (codes or {}).items():
            _mask_ioctl(fd, t, sum(1 << c for c in set(allowed)))
        _mask_ioctl(fd, 0, (1 << 32) - 1 if types is None else sum(1 << t for t in set(types)))
        return True
    except OSError:
        return False


def _sandboxed() -> bool:
    """Under tests/_sandbox.py: never read (let alone grab) the user's real controllers."""
    return bool(os.environ.get("MOMENTO_TEST_SANDBOX"))


def open_device(path: str):
    """Open ``path`` with python-evdev; the device if it is a gamepad, else None."""
    evdev = _import_evdev()
    if evdev is None or _sandboxed():
        return None
    if _sysfs_has_key(path, BTN_SOUTH) is False:
        return None
    dev = evdev.InputDevice(path)  # OSError (EACCES, ENODEV) propagates
    try:
        if not is_gamepad_caps(dev.capabilities()):
            dev.close()
            return None
    except OSError:
        dev.close()
        raise
    dev.driver = _sysfs_driver(path)
    return dev


# ------------------------------------------------------------------ inotify
class _Inotify:
    """Minimal inotify on /dev/input (ctypes); None-safe fallback is polling."""

    IN_ATTRIB, IN_MOVED_FROM, IN_MOVED_TO = 0x4, 0x40, 0x80
    IN_CREATE, IN_DELETE = 0x100, 0x200

    def __init__(self, directory: str):
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        mask = self.IN_ATTRIB | self.IN_MOVED_FROM | self.IN_MOVED_TO | self.IN_CREATE | self.IN_DELETE
        if libc.inotify_add_watch(fd, directory.encode(), mask) < 0:
            err = ctypes.get_errno()
            os.close(fd)
            raise OSError(err, "inotify_add_watch")
        self.fd = fd
        self.directory = directory

    def read(self) -> list[tuple[str, int]]:
        out = []
        while True:
            try:
                data = os.read(self.fd, 8192)
            except BlockingIOError:
                break
            if not data:
                break
            pos = 0
            while pos + 16 <= len(data):
                _wd, mask, _cookie, size = struct.unpack_from("iIII", data, pos)
                name = data[pos + 16:pos + 16 + size].split(b"\0", 1)[0].decode(errors="replace")
                out.append((name, mask))
                pos += 16 + size
        return out

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# --------------------------------------------------------------- per-pad state
class _Pad:
    __slots__ = ("dev", "key", "name", "fd", "layout", "symbols", "names", "has_hat", "axes_info",
                 "held", "axes", "hat", "dir", "dir_src", "dir_blocked", "repeat_at",
                 "chord_since", "chord_latched", "grabbed", "grab_failed", "grab_wait_since",
                 "busy_since", "grab_poll_at", "abs_hat", "mask", "mod_held", "mod_grabbed",
                 "mod_since", "mod_check_at", "dropped", "trig_axes", "trig_latched",
                 "origin", "mono_ts", "grab_batch", "grab_at", "dpad_at", "use", "mod_at")

    def __init__(self, dev, key, name="", layout="standard", has_hat=True, axes_info=None,
                 abs_hat=None, symbols="xbox"):
        self.dev = dev
        self.key = key
        self.name = name
        self.fd = None
        self.layout = layout
        self.symbols = symbols             # SYMBOLS key: the labels its buttons carry
        self.has_hat = has_hat
        self.axes_info = axes_info or {}   # code -> (min, max)
        names = dict(_COMMON)
        if layout == "xbox":
            names[BTN_X], names[BTN_Y] = "west", "north"
        else:
            names[BTN_NORTH], names[BTN_WEST] = "north", "west"
        names.update(_HAPPY_EXTRA if has_hat else _HAPPY_DPAD)
        self.names = names                 # code -> button name
        self.held: set[int] = set()
        self.axes: dict[int, float] = {}   # normalised: sticks -1..1, triggers 0..1
        self.hat = [0, 0]
        self.dir = None
        self.dir_src = None                # "digital" | "stick"
        self.dir_blocked = False           # held when the pad was opened: no action until it changes
        self.repeat_at = None
        self.chord_since = None
        self.chord_latched = False
        self.grabbed = False
        self.grab_failed = False
        self.grab_wait_since = None
        self.busy_since = None             # first EBUSY of the current grab attempt
        self.grab_poll_at = None           # next re-read while a grab waits
        # the D-pad is a hat (ABS_HAT0X/Y): the key-only mask hides it
        self.abs_hat = (ABS_HAT0X in self.axes_info or ABS_HAT0Y in self.axes_info) if abs_hat is None \
            else abs_hat
        self.mask = "unset"                # the event mask last set on the fd
        self.mod_held = False              # the chord's modifier (e.g. mode) is down
        self.mod_grabbed = False           # ...and we hold the pad for it (chord_grab)
        self.mod_since = None
        self.mod_check_at = None           # next re-read of the keys while the modifier is held
        self.dropped = False
        self.trig_axes: set[int] = set()   # ABS_Z/ABS_RZ that behave as analog triggers
        self.trig_latched = [False, False] # per TRIGGERS entry: pulled (fired), not yet re-armed
        self.origin = ""                   # device_origin(): physical / virtual (who made it)
        self.mono_ts = False               # event timestamps are on the hub's clock
        self.grab_batch = -1               # the read during which the shortcut's grab was taken
        self.grab_at = None                # ...and when it took effect
        self.dpad_at: dict = {}            # chord direction held -> (pressed at, hidden, why not)
        self.use = None                    # the shortcut use in progress (see _flush_use)
        self.mod_at = None                 # when the modifier went down (None: before we opened it)

    def held_names(self) -> set[str]:
        return {self.names[c] for c in self.held if c in self.names}

    def chord_names(self) -> set[str]:
        """``held_names()`` plus the hat as ``dpad_*`` (what a chord matches)."""
        names = self.held_names()
        x, y = self.hat
        if x:
            names.add("dpad_right" if x > 0 else "dpad_left")
        if y:
            names.add("dpad_down" if y > 0 else "dpad_up")
        return names

    def normalize(self, code: int, value: int) -> float:
        lo, hi = self.axes_info.get(code, (-32768, 32767))
        if hi <= lo:
            return 0.0
        if lo >= 0 and code in (ABS_Z, ABS_RZ):          # trigger: 0..1
            return (value - lo) / (hi - lo)
        mid = (lo + hi) / 2.0
        return max(-1.0, min(1.0, (value - mid) / ((hi - lo) / 2.0)))


def _pick(x: int, y: int, cur):
    hx = ("right" if x > 0 else "left") if x else None
    hy = ("down" if y > 0 else "up") if y else None
    if hx and hy:
        return cur if cur in (hx, hy) else hy
    return hx or hy


# ------------------------------------------------------------------- the core
_live: "weakref.WeakSet[Gamepads]" = weakref.WeakSet()


@atexit.register
def _release_all_at_exit() -> None:
    for hub in list(_live):
        try:
            hub.ungrab()
        except Exception:
            pass


class Gamepads:
    """All connected gamepads as one input source. See the module docstring."""

    def __init__(self, *, on_action: Callable[[str, bool], None] | None = None,
                 on_button: Callable[[str, bool], None] | None = None,
                 on_chord: Callable[[], None] | None = None,
                 on_grab_lost: Callable[[], None] | None = None,
                 on_devices: Callable[[], None] | None = None,
                 on_active: Callable[[], None] | None = None,
                 navigate: bool = True, chord=DEFAULT_CHORD, hold_ms: int = DEFAULT_HOLD_MS,
                 deadzone: float = DEADZONE, repeat_delay_ms: int = REPEAT_DELAY_MS,
                 repeat_interval_ms: int = REPEAT_INTERVAL_MS, watchdog_s: float = WATCHDOG_S,
                 grab_wait_s: float = GRAB_WAIT_S, rescan_s: float = RESCAN_S,
                 grab_busy_s: float = GRAB_BUSY_S, chord_grab: bool = False,
                 chord_grab_s: float = CHORD_GRAB_S,
                 clock: Callable[[], float] = time.monotonic,
                 lister: Callable[[], list[str]] | None = None,
                 opener: Callable[[str], object] | None = None,
                 hotplug: str = "auto", watchdog_thread: bool = True):
        self.on_action = on_action
        self.on_button = on_button
        self.on_chord = on_chord
        self.on_grab_lost = on_grab_lost
        self.on_devices = on_devices
        self.on_active = on_active
        self.navigate = navigate
        self.chord: tuple[str, ...] = ()
        self.chord_mod: tuple[str, ...] = ()    # the chord minus its D-pad directions
        self.chord_dpad = False                 # the chord has a D-pad direction
        self.hold = 0.0
        # take a pad while the chord's modifier is held (the daemon; see the module doc)
        self.chord_grab = bool(chord_grab)
        self.chord_grab_s = chord_grab_s
        self.grab_busy_s = grab_busy_s
        self.set_chord(chord, hold_ms)
        self.deadzone = deadzone
        self.repeat_delay = repeat_delay_ms / 1000.0
        self.repeat_interval = repeat_interval_ms / 1000.0
        self.watchdog_s = watchdog_s
        self.grab_wait_s = grab_wait_s
        self.rescan_s = rescan_s
        self.clock = clock
        self._lister = lister or list_candidates
        self._opener = opener or open_device
        self._hotplug_mode = hotplug       # "auto" (inotify, else poll) | "poll" | "off"
        self._watchdog_thread_on = watchdog_thread

        self.pads: dict[object, _Pad] = {}      # key (path) -> pad
        self._by_fd: dict[int, _Pad] = {}
        self._scanned: set[str] = set()        # keys opened by rescan() (so it may drop them)
        self._skip: dict[str, tuple] = {}       # path -> stat signature of a node that isn't a pad
        self._inotify: _Inotify | None = None
        self._next_rescan = None
        self._started = False
        self._closed = False

        self._lock = threading.RLock()
        self._want_grab = False
        self._activity = 0.0
        self._grab_lost_pending = False
        self._wd_thread = None
        self._wd_stop = threading.Event()

        self._last_action: dict[str, tuple[float, object]] = {}
        self._last_chord: tuple[float, object] | None = None
        self._batch = 0                                   # reads so far (one per process/feed)
        self._ev_ts = None                                # the event being handled: kernel time
        self.last_chord_key = None                        # the pad the last chord came from
        self.last_input_key = None                        # the pad in use (see active_pad)
        self._input_at = 0.0                              # ...its last press
        self._listeners: list[Callable[[], None]] = []   # loop adapters: fds/timer changed
        _live.add(self)

    # ---------------------------------------------------------- configuration
    def set_chord(self, buttons, hold_ms: int | None = None) -> None:
        """Buttons that must be held together for ``hold_ms`` (0: fires the moment
        they are all down); ``()``/None disables."""
        self.chord = normalize_chord(buttons) if buttons else ()
        self.chord_dpad = any(n in DPAD_NAMES for n in self.chord)
        self.chord_mod = tuple(n for n in self.chord if n not in DPAD_NAMES) if self.chord_dpad else ()
        if hold_ms is not None:
            self.hold = max(0, int(hold_ms)) / 1000.0
        for pad in list(getattr(self, "pads", {}).values()):
            pad.chord_since = None
            self._mod_release(pad)
            pad.mod_held = False
            pad.mod_check_at = None
            pad.use, pad.mod_at, pad.dpad_at = None, None, {}
            self._apply_mask(pad)
            pad.chord_latched = self._chord_held(pad)

    def set_chord_grab(self, on: bool) -> None:
        """Hold a pad while the chord's modifier is down (see the module doc)."""
        self.chord_grab = bool(on)
        if not on:
            for pad in list(self.pads.values()):
                self._mod_release(pad)

    def set_navigate(self, on: bool) -> None:
        """Turn action/raw-button reporting on (the bar) or off (daemon: chord only)."""
        self.navigate = bool(on)
        for pad in self.pads.values():
            self._apply_mask(pad)
            if not on:
                pad.repeat_at = None
            else:
                # axes were masked out: take their state now so a stale value fires nothing
                self._resync(pad)
                self._seed_triggers(pad)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Open the pads that are connected now and start watching for hotplug.

        Returns False (and does nothing else) when python-evdev is missing and no
        custom ``opener`` was given.
        """
        if self._started:
            return True
        if self._opener is open_device and not available():
            _warn_once("no-evdev", "python-evdev is not installed; controller support is off",
                       level=logging.INFO)
            return False
        if self._opener is open_device and _sandboxed():
            log.debug("test sandbox: not opening real controllers")
            return False
        self._started = True
        if self._hotplug_mode == "auto" and self._lister is list_candidates:
            try:
                self._inotify = _Inotify(INPUT_DIR)
            except (OSError, AttributeError) as e:
                log.debug("inotify on %s unavailable (%s); polling every %.0f s", INPUT_DIR, e, self.rescan_s)
        self.rescan()
        return True

    def close(self) -> None:
        """Ungrab and close everything (idempotent)."""
        if self._closed:
            return
        self._closed = True
        try:
            self.ungrab()
        finally:
            for key in list(self.pads):
                self._drop(key, notify=False)
            if self._inotify is not None:
                self._inotify.close()
                self._inotify = None
            self._started = False
            self._changed()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ devices
    def fds(self) -> list[int]:
        out = [p.fd for p in self.pads.values() if p.fd is not None]
        if self._inotify is not None:
            out.append(self._inotify.fd)
        return out

    def devices(self) -> list[dict]:
        return [{"key": p.key, "name": p.name, "layout": p.layout, "symbols": p.symbols,
                 "grabbed": p.grabbed} for p in self.pads.values()]

    def active_pad(self) -> _Pad | None:
        """The pad in use: the last one that sent a press, else the only one connected."""
        pad = self.pads.get(self.last_input_key)
        if pad is None and len(self.pads) == 1:
            pad = next(iter(self.pads.values()))
        return pad

    def symbols(self) -> str:
        """The button names the hints show (a SYMBOLS key): the pad in use's; with
        several pads and none used yet, theirs when they agree, else "xbox"."""
        pad = self.active_pad()
        if pad is not None:
            return pad.symbols
        kinds = {p.symbols for p in self.pads.values()}
        return kinds.pop() if len(kinds) == 1 else "xbox"

    def _note_input(self, pad: _Pad, now: float) -> None:
        """A press on ``pad``: it becomes the pad in use (``on_active`` when that changes).
        Another pad's press this soon after the current one's is a mirror (Steam)."""
        if pad.key == self.last_input_key:
            self._input_at = now
            return
        if self.last_input_key in self.pads and now - self._input_at < CHORD_DEDUP_S:
            return
        before = self.active_pad()
        self.last_input_key, self._input_at = pad.key, now
        if before is not pad and self.on_active:
            self._safe(self.on_active)

    def rescan(self, paths: Iterable[str] | None = None) -> None:
        """Open new pads and drop vanished ones (all candidates, or just ``paths``)."""
        now = self.clock()
        if self._hotplug_mode == "off" or (self._hotplug_mode == "auto" and self._inotify is not None):
            self._next_rescan = None
        else:
            self._next_rescan = now + self.rescan_s
        full = paths is None
        wanted = list(self._lister()) if full else list(paths)
        changed = False
        if full:
            for key in [k for k in self._scanned if k not in wanted]:
                self._drop(key, notify=False)
                changed = True
        for path in wanted:
            if path in self.pads:
                continue
            sig = self._stat_sig(path)
            if sig is not None and self._skip.get(path) == sig:
                continue
            try:
                dev = self._opener(path)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM):
                    _warn_once(("noperm", path), "can't read %s (%s); skipping it", path,
                               e.strerror, level=logging.DEBUG)
                if sig is not None:
                    self._skip[path] = sig
                continue
            if dev is None:
                if sig is not None:
                    self._skip[path] = sig
                continue
            self._skip.pop(path, None)
            self.add_device(dev, key=path, notify=False)
            self._scanned.add(path)
            changed = True
        if changed:
            self._changed()
            if self.on_devices:
                self._safe(self.on_devices)

    @staticmethod
    def _stat_sig(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_rdev, st.st_ctime_ns)

    def add_device(self, dev, key=None, notify: bool = True) -> _Pad:
        """Adopt an open device (python-evdev InputDevice or :class:`FakeDevice`)."""
        key = key if key is not None else getattr(dev, "path", None) or id(dev)
        if key in self.pads:
            self._drop(key, notify=False)
        caps = {}
        try:
            caps = dev.capabilities(absinfo=True)
        except TypeError:
            caps = dev.capabilities()
        except OSError:
            pass
        axes_info, has_hat = {}, False
        for item in caps.get(EV_ABS, ()):
            code, info = item if isinstance(item, tuple) else (item, None)
            if code in (ABS_HAT0X, ABS_HAT0Y):
                has_hat = True
            if info is not None:
                axes_info[code] = (info.min, info.max)
        # BTN_TRIGGER_HAPPY1-4 are the D-pad only on pads that have no other one (xpad's
        # D-pad-as-buttons); hid-steam, for one, has BTN_DPAD_* and uses them for other buttons.
        has_hat = has_hat or BTN_DPAD_UP in caps.get(EV_KEY, ())
        vendor = getattr(getattr(dev, "info", None), "vendor", 0) or 0
        name = getattr(dev, "name", "") or str(key)
        driver = getattr(dev, "driver", "") or ""
        layout = layout_for(vendor, driver, name)
        pad = _Pad(dev, key, name=name, layout=layout, has_hat=has_hat, axes_info=axes_info,
                   symbols=symbols_for(vendor, driver, name))
        try:
            pad.fd = dev.fileno()
        except (AttributeError, OSError, ValueError):
            pad.fd = getattr(dev, "fd", None)
        info = getattr(dev, "info", None)
        pad.origin = device_origin(str(key), name, getattr(dev, "phys", "") or "", vendor,
                                   getattr(info, "product", 0) or 0)
        setter = getattr(dev, "set_clock_monotonic", None)   # FakeDevice
        if setter is not None:
            pad.mono_ts = bool(setter())
        elif pad.fd is not None and self.clock is time.monotonic:
            pad.mono_ts = set_monotonic_clock(pad.fd)
        self._seed(pad, caps)
        if pad.mod_held and not self.navigate:
            pad.mod_check_at = self.clock() + self.chord_grab_s
        self.pads[key] = pad
        if pad.fd is not None:
            self._by_fd[pad.fd] = pad
        self._apply_mask(pad)
        log.info("controller connected: %s (%s, %s layout%s)", name, key, layout,
                 f", {pad.origin}" if pad.origin else "")
        with self._lock:
            if self._want_grab:
                self._try_grab(pad, self.clock())
        if notify:
            self._changed()
            if self.on_devices:
                self._safe(self.on_devices)
        return pad

    def remove_device(self, key) -> None:
        self._drop(key, notify=True)

    def _drop(self, key, notify: bool) -> None:
        self._scanned.discard(key)
        pad = self.pads.pop(key, None)
        if pad is None:
            return
        if pad.fd is not None and self._by_fd.get(pad.fd) is pad:
            del self._by_fd[pad.fd]
        self._mod_release(pad)
        if pad.grabbed:
            try:
                pad.dev.ungrab()
            except Exception:
                pass
            pad.grabbed = False
        try:
            pad.dev.close()
        except Exception:
            pass
        log.info("controller disconnected: %s (%s)", pad.name, key)
        if notify:
            self._changed()
            if self.on_devices:
                self._safe(self.on_devices)

    def _seed(self, pad: _Pad, caps) -> None:
        """Take the pad's current state silently: nothing held now fires later."""
        dev = pad.dev
        try:
            pad.held = {c for c in dev.active_keys() if c in pad.names}
        except Exception:
            pad.held = set()
        for code in list(pad.axes_info):
            try:
                value = dev.absinfo(code).value
            except Exception:
                continue
            if code in (ABS_HAT0X, ABS_HAT0Y):
                pad.hat[code - ABS_HAT0X] = (value > 0) - (value < 0)
            else:
                pad.axes[code] = pad.normalize(code, value)
        pad.dir, pad.dir_src = self._direction(pad)
        pad.dir_blocked = pad.dir is not None
        pad.chord_latched = self._chord_held(pad)
        pad.mod_held = self._mod_held(pad)
        if self.chord_dpad and self.chord_mod:
            pad.dpad_at = {n: (None, False, "held when the controller was connected")
                           for n in self._chord_dirs(pad)}
        pad.trig_axes = {code for _a, _b, code in TRIGGERS
                         if pad.axes_info.get(code, (-1, 0))[0] >= 0
                         and pad.axes.get(code, 0.0) < TRIGGER_RELEASE}
        self._seed_triggers(pad)

    def _seed_triggers(self, pad: _Pad) -> None:
        """A trigger pulled right now is taken as already fired: it acts once let go."""
        pad.trig_latched = [self._trigger_pulled(pad, i, True) for i in range(len(TRIGGERS))]

    def _mask_for(self, pad: _Pad):
        if self.navigate:
            return None
        # the hat is needed only for a D-pad chord (always: its presses tell the order
        # of PS and Down); HAT_ONLY keeps the sticks out
        if self.chord_dpad and pad.abs_hat:
            return (EV_KEY, EV_ABS)
        return (EV_KEY,)

    def _apply_mask(self, pad: _Pad) -> None:
        if pad.dev is None or pad.fd is None:
            return
        types = self._mask_for(pad)
        if types == pad.mask:
            return
        widened = pad.mask != "unset" and pad.mask is not None and EV_ABS not in pad.mask \
            and types is not None and EV_ABS in types
        pad.mask = types
        codes = HAT_ONLY if types is not None and EV_ABS in types else None
        setter = getattr(pad.dev, "set_event_mask", None)  # FakeDevice
        if setter is not None:
            setter(types, codes)
        else:
            set_event_mask(pad.fd, types, codes)
        if widened:                        # the hat was hidden until now: read it
            self._resync_axes(pad, (ABS_HAT0X, ABS_HAT0Y))

    # --------------------------------------------------------- input path
    def process(self, fd: int, now: float | None = None) -> None:
        """Handle whatever is readable on ``fd`` (a pad or the hotplug watch)."""
        if self._inotify is not None and fd == self._inotify.fd:
            self._on_inotify()
            return
        pad = self._by_fd.get(fd)
        if pad is None:
            return
        try:
            events = list(pad.dev.read())
        except BlockingIOError:
            return
        except OSError as e:  # ENODEV: unplugged
            log.debug("read %s: %s", pad.key, e)
            self._drop(pad.key, notify=True)
            return
        now = self.clock() if now is None else now
        self._batch += 1
        try:
            for ev in events:
                ts = None
                if pad.mono_ts:
                    sec, usec = getattr(ev, "sec", None), getattr(ev, "usec", None)
                    if sec is not None and usec is not None:
                        ts = sec + usec / 1e6
                self._handle(pad, ev.type, ev.code, ev.value, now, ts)
        except Exception:
            log.exception("controller input handling failed; releasing the controllers")
            self.ungrab()
        self._changed(timer_only=True)

    def feed(self, pad, type_: int, code: int, value: int, now: float | None = None) -> None:
        """Push one synthetic event (tests, or a caller with its own reader)."""
        if not isinstance(pad, _Pad):
            pad = self.pads[pad]
        self._batch += 1
        self._handle(pad, type_, code, value, self.clock() if now is None else now)

    def _handle(self, pad: _Pad, t: int, code: int, value: int, now: float,
                ts: float | None = None) -> None:
        self._ev_ts = ts
        if t == EV_SYN:
            if code == SYN_DROPPED:
                pad.dropped = True
            elif code == SYN_REPORT:
                if pad.dropped:
                    pad.dropped = False
                    self._resync(pad)
                self._after_report(pad, now)
            return
        if pad.dropped:
            return
        if self._want_grab:
            self._activity = now
        if t == EV_KEY:
            if value == 2 or code not in pad.names:   # kernel key repeat / unknown key
                return
            name = pad.names[code]
            if value:
                pad.held.add(code)
                self._note_input(pad, now)
            else:
                pad.held.discard(code)
            if name in DPAD_BUTTONS:
                self._track_dpad(pad, now)
            if self.chord_mod:
                self._update_modifier(pad, now)
            if name in DPAD_BUTTONS:
                self._update_dir(pad, now)
            elif self.navigate and value and name in BUTTON_ACTIONS:
                self._emit(pad, BUTTON_ACTIONS[name], False, now)
            elif name in ("tl2", "tr2"):
                self._update_triggers(pad, now)
            if self.navigate and self.on_button:
                self._call(self.on_button, name, bool(value))
            self._update_chord(pad, now)
            self._check_pending_grab(pad, now)
        elif t == EV_ABS:
            if code in (ABS_HAT0X, ABS_HAT0Y):
                pad.hat[code - ABS_HAT0X] = (value > 0) - (value < 0)
                if self.chord_dpad:
                    self._track_dpad(pad, now)
                    self._update_chord(pad, now)
            else:
                pad.axes[code] = pad.normalize(code, value)

    def _after_report(self, pad: _Pad, now: float) -> None:
        self._update_dir(pad, now)
        self._update_triggers(pad, now)
        self._check_pending_grab(pad, now)

    def _resync(self, pad: _Pad, axes: bool = True) -> None:
        try:
            pad.held = {c for c in pad.dev.active_keys() if c in pad.names}
        except Exception:
            pass
        if axes:
            self._resync_axes(pad)

    def _resync_axes(self, pad: _Pad, codes=None) -> None:
        for code in list(pad.axes_info) if codes is None else codes:
            if code not in pad.axes_info:
                continue
            try:
                value = pad.dev.absinfo(code).value
            except Exception:
                continue
            if code in (ABS_HAT0X, ABS_HAT0Y):
                pad.hat[code - ABS_HAT0X] = (value > 0) - (value < 0)
            else:
                pad.axes[code] = pad.normalize(code, value)

    @staticmethod
    def _pending(pad: _Pad) -> bool:
        """Events are waiting on the fd (they will bring our state up to date)."""
        import select

        try:
            return bool(select.select([pad.fd], [], [], 0)[0])
        except (OSError, ValueError, TypeError):
            return False

    def _resync_blind(self, pad: _Pad) -> None:
        """Re-read the pad from the kernel: another program's grab may have hidden events.

        Skipped while events are queued (then we aren't blind, and they come next).
        Nothing fires from what is read here: a direction that changed is blocked
        until it changes again; the chord only lets go.
        """
        if pad.dev is None or self._pending(pad):
            return
        self._resync(pad)
        d, src = self._direction(pad)
        if d != pad.dir:
            pad.dir, pad.dir_src = d, src
            pad.dir_blocked = d is not None
            pad.repeat_at = None
        if not self._chord_held(pad):
            pad.chord_since = None
            pad.chord_latched = False
        pad.mod_held = self._mod_held(pad)

    # ------------------------------------------------------ directions/repeat
    def _direction(self, pad: _Pad):
        names = pad.held_names()
        x = pad.hat[0] or (("dpad_right" in names) - ("dpad_left" in names))
        y = pad.hat[1] or (("dpad_down" in names) - ("dpad_up" in names))
        if x or y:
            return _pick(x, y, pad.dir), "digital"
        sx, sy = pad.axes.get(ABS_X, 0.0), pad.axes.get(ABS_Y, 0.0)
        if pad.dir_src == "stick" and pad.dir:
            keep = self.deadzone * RELEASE_FRACTION
            v = {"left": -sx, "right": sx, "up": -sy, "down": sy}[pad.dir]
            if v >= keep and v >= max(abs(sx), abs(sy)) * 0.5:
                return pad.dir, "stick"
        if max(abs(sx), abs(sy)) < self.deadzone:
            return None, None
        if abs(sx) > abs(sy):
            return ("right" if sx > 0 else "left"), "stick"
        return ("down" if sy > 0 else "up"), "stick"

    def _update_dir(self, pad: _Pad, now: float) -> None:
        d, src = self._direction(pad)
        pad.dir_src = src
        if d == pad.dir:
            return
        pad.dir = d
        pad.dir_blocked = False
        if d is None:
            pad.repeat_at = None
            return
        if pad.mod_held:
            # the shortcut's modifier is down: the D-pad belongs to the shortcut
            pad.dir_blocked = True
            pad.repeat_at = None
            return
        if self.navigate:
            self._emit(pad, d, False, now)
            pad.repeat_at = now + self.repeat_delay
        else:
            pad.repeat_at = None

    # ------------------------------------------------------------- triggers
    def _trigger_pulled(self, pad: _Pad, i: int, latched: bool) -> bool:
        _action, button, axis = TRIGGERS[i]
        if button in pad.held:
            return True
        if axis not in pad.trig_axes:
            return False
        return pad.axes.get(axis, 0.0) >= (TRIGGER_RELEASE if latched else TRIGGER_PRESS)

    def _update_triggers(self, pad: _Pad, now: float) -> None:
        """One action per pull: the button or the axis latches, both must let go to re-arm."""
        if not self.navigate:
            return
        for i, (action, _button, _axis) in enumerate(TRIGGERS):
            pulled = self._trigger_pulled(pad, i, pad.trig_latched[i])
            if pulled == pad.trig_latched[i]:
                continue
            pad.trig_latched[i] = pulled
            if pulled:
                self._emit(pad, action, False, now)

    def _emit(self, pad: _Pad, action: str, repeat: bool, now: float) -> None:
        if not repeat:
            self._note_input(pad, now)          # a stick, the hat or a trigger axis counts too
        last = self._last_action.get(action)
        if last is not None and last[1] != pad.key and now - last[0] < ACTION_DEDUP_S:
            return
        self._last_action[action] = (now, pad.key)
        if self.on_action:
            self._call(self.on_action, action, repeat)

    # ------------------------------------------------------------- chord
    def _chord_held(self, pad: _Pad) -> bool:
        return self._all_held(pad.chord_names() if self.chord_dpad else pad.held_names(), self.chord)

    def _mod_held(self, pad: _Pad) -> bool:
        return self._all_held(pad.held_names(), self.chord_mod)

    @staticmethod
    def _all_held(names: set[str], wants) -> bool:
        if not wants:
            return False
        for want in wants:
            options = GROUPS.get(want, (want,))
            if not any(o in names for o in options):
                return False
        return True

    # ------------------------------------------- the D-pad chord's modifier
    def _chord_dirs(self, pad: _Pad) -> set[str]:
        """The chord's D-pad directions held now (hat or buttons)."""
        return {n for n in pad.chord_names() if n in DPAD_NAMES and n in self.chord}

    def _ev_time(self, now: float) -> float:
        """When the event being handled happened: its kernel time, else the read's."""
        return self._ev_ts if self._ev_ts is not None else now

    def _why_seen(self, pad: _Pad) -> str | None:
        """Why a D-pad press arriving now also reaches other readers (None: it doesn't)."""
        if pad.grabbed:
            return None                     # the bar's own grab
        use = pad.use
        if not pad.mod_held or use is None:
            return f"pressed before {self._mod_word(pad)}"
        if pad.mod_grabbed:
            if pad.grab_batch >= self._batch:
                return f"it came in the same read as {self._mod_word(pad)}, queued before the grab"
            if self._ev_ts is not None and pad.grab_at is not None and self._ev_ts < pad.grab_at:
                return "its kernel time is before the grab"
            return None
        if not self.chord_grab:
            return "exclusive is off"
        if use["grab_err"]:
            return f"grab failed: {use['grab_err']}"
        if use["grab_end"] is not None:
            return "the grab was already let go"
        return "not grabbed"

    def _mod_word(self, pad: _Pad) -> str:
        return "+".join(button_word(m, pad.symbols) for m in self.chord_mod)

    def _track_dpad(self, pad: _Pad, now: float) -> None:
        """A D-pad change: note when each of the chord's directions went down and
        whether other readers (the game) saw it; let go of a grab that no longer helps."""
        if not (self.chord_dpad and self.chord_mod):
            return
        dirs = self._chord_dirs(pad)
        for n in [n for n in pad.dpad_at if n not in dirs]:
            del pad.dpad_at[n]
        at = self._ev_time(now)
        leaked = False
        for n in sorted(dirs - set(pad.dpad_at)):
            why = self._why_seen(pad)
            pad.dpad_at[n] = (at, why is None, why)
            leaked = leaked or (why is not None and pad.mod_grabbed)
        if leaked:
            # the game saw this press: hiding its release would leave it stuck
            self._mod_release(pad, why="leaked")
        elif not dirs:
            if pad.mod_grabbed and not pad.mod_held:
                self._mod_release(pad)       # the hold for the direction ends with its release
            elif (pad.mod_held and pad.use is not None and pad.use["skipped"]
                  and self.chord_grab and not self._want_grab):
                pad.use["skipped"] = False   # the direction held before PS is let go:
                self._mod_grab(pad, now)     # hide the next one

    def _update_modifier(self, pad: _Pad, now: float) -> None:
        """The chord's modifier went down or up: hold/let go of the pad."""
        held = self._mod_held(pad)
        if held == pad.mod_held:
            return
        pad.mod_held = held
        if held:
            self._flush_use(pad, force=True)  # (a grab still kept for the last Down)
            pad.mod_at = self._ev_time(now)
            pad.use = {"grab_at": pad.grab_at if pad.mod_grabbed else None, "grab_end": None,
                       "released": None, "grab_err": None, "skipped": False, "line": None}
            if self.chord_grab and not self._want_grab and not pad.mod_grabbed:
                if self._chord_dirs(pad):
                    # already seen by the game: hiding its release would leave it stuck
                    pad.use["skipped"] = True
                else:
                    self._mod_grab(pad, now)
            self._apply_mask(pad)
            if not self.navigate or pad.mod_grabbed:
                pad.mod_check_at = now + self.chord_grab_s
        else:
            pad.mod_check_at = None
            if pad.mod_grabbed and self._chord_dirs(pad):
                # the direction is still down: the game saw neither its press nor its
                # release yet, so keep the pad until it is let go (the watchdog caps it)
                pad.mod_check_at = (pad.mod_since if pad.mod_since is not None else now) + self.chord_grab_s
            else:
                self._mod_release(pad)
            self._apply_mask(pad)
            self._flush_use(pad)

    def _mod_grab(self, pad: _Pad, now: float) -> None:
        with self._lock:
            if pad.mod_grabbed or pad.grabbed or pad.fd is None:
                return
            try:
                pad.dev.grab()
            except OSError as e:
                err = e.strerror or str(e)
                if pad.use is not None:
                    pad.use["grab_err"] = err
                _warn_once(("modgrab", pad.key, pad.name),
                           "can't hold %s (%s%s) for the controller shortcut: %s%s; "
                           "the game will also see the D-pad press", pad.name, pad.key,
                           f", {pad.origin}" if pad.origin else "", err,
                           " (another program holds it, e.g. InputPlumber or Steam)"
                           if e.errno == errno.EBUSY else "", level=logging.INFO)
                return
            pad.mod_grabbed = True
            pad.mod_since = now
            pad.grab_batch = self._batch
            pad.grab_at = self.clock()
            if pad.use is not None:
                pad.use.update(grab_at=pad.grab_at, grab_end=None, released=None)
        self._start_watchdog_thread()

    def _mod_release(self, pad: _Pad, why: str | None = None) -> None:
        with self._lock:
            if not pad.mod_grabbed:
                return
            pad.mod_grabbed = False
            pad.mod_since = None
            try:
                pad.dev.ungrab()
            except Exception as e:  # already gone
                log.debug("ungrab %s: %s", pad.key, e)
            use = pad.use
            if use is not None and use["grab_at"] is not None and use["grab_end"] is None:
                use["grab_end"], use["released"] = self.clock(), why
            if not pad.mod_held:
                pad.mod_check_at = None
        self._flush_use(pad)

    def _flush_use(self, pad: _Pad, force: bool = False) -> None:
        """Log the shortcut use once its grab is over (one line per use)."""
        use = pad.use
        if use is None:
            return
        if use["line"] is not None and (force or not pad.mod_grabbed):
            line, use["line"] = use["line"], None
            if use["grab_at"] is not None:
                if use["grab_end"] is None:
                    line += ", grab still held"
                else:
                    line += f", grab held {(use['grab_end'] - use['grab_at']) * 1000:.0f} ms"
                    if use["released"] == "watchdog":
                        line += f" (let go by the {self.chord_grab_s:g} s watchdog)"
                    elif use["released"] == "leaked":
                        line += " (let go at once, so the game also sees the release)"
            log.info("%s", line)
        if not pad.mod_held and not pad.mod_grabbed:
            pad.use = None

    def _order(self, pad: _Pad):
        """The order rule for a D-pad chord with a modifier: ("ignore" | "fire", line),
        or None for other chords. See the module doc, "Order"."""
        if not (self.chord_dpad and self.chord_mod):
            return None
        mod_w = self._mod_word(pad)
        entries = [(pad.dpad_at.get(n, (None, False, "read from the kernel")), n)
                   for n in sorted(self._chord_dirs(pad))]
        (d_at, hidden, why), n = max(entries, key=lambda e: -1e18 if e[0][0] is None else e[0][0])
        dir_w = button_word(n, pad.symbols)
        who = f"{pad.name} ({pad.key}{', ' + pad.origin if pad.origin else ''})"
        if d_at is None:
            return ("ignore", f"shortcut ignored: {who}, order {dir_w}->{mod_w} "
                              f"({dir_w} already held before {mod_w}): a game press, the bar stays closed")
        if pad.mod_at is None:
            order = f"order {mod_w}->{dir_w} ({mod_w} held since the controller was connected)"
        else:
            delta = d_at - pad.mod_at
            ms = abs(delta) * 1000
            if delta < -ORDER_TOLERANCE_S - 1e-9:
                return ("ignore", f"shortcut ignored: {who}, order {dir_w}->{mod_w} ({dir_w} {ms:.0f} ms "
                                  f"before {mod_w}): a game press, the bar stays closed")
            if delta < 0:
                order = (f"order {dir_w}->{mod_w} ({dir_w} {ms:.0f} ms before {mod_w}, "
                         f"within {ORDER_TOLERANCE_S * 1000:.0f} ms: counted as together)")
            else:
                order = f"order {mod_w}->{dir_w} ({dir_w} {ms:.0f} ms after {mod_w})"
        if hidden:
            state = f"grabbed before {dir_w}: yes"
        else:
            state = f"grabbed before {dir_w}: no ({why}): {dir_w} reached other apps"
        return ("fire", f"shortcut: {who}, {order}, {state}")

    def _check_modifier(self, pad: _Pad, now: float) -> None:
        """Timer while the modifier is held (or its grab kept for the D-pad): the grab
        watchdog, and a re-read of the keys (we may have missed the release while
        someone else held the pad)."""
        if pad.mod_grabbed:
            log.warning("controller shortcut: %s held for %.0f s; giving the controller back",
                        pad.name, self.chord_grab_s)
            self._mod_release(pad, why="watchdog")
        if not pad.mod_held:
            pad.mod_check_at = None
            return
        if not self._pending(pad):
            self._resync(pad, axes=False)
        if self._mod_held(pad):
            pad.mod_check_at = now + self.chord_grab_s
            return
        pad.mod_held = True                # (so the update below sees the release)
        self._update_modifier(pad, now)

    def _update_chord(self, pad: _Pad, now: float) -> None:
        if not self._chord_held(pad):
            pad.chord_since = None
            pad.chord_latched = False
            return
        if pad.chord_latched:
            return
        if pad.chord_since is None:
            pad.chord_since = now
        if now - pad.chord_since >= self.hold - 1e-9:
            pad.chord_latched = True
            pad.chord_since = None
            order = self._order(pad)
            if order is not None and order[0] == "ignore":
                log.info("%s", order[1])      # latched: not again until it is let go
                return
            mirror = (self._last_chord and self._last_chord[1] != pad.key
                      and now - self._last_chord[0] < CHORD_DEDUP_S)
            if order is not None:
                line = order[1]
                if mirror:
                    line += " [the same press as on another controller: counted once]"
                if pad.use is None:            # (a use without its modifier's press: seeded)
                    pad.use = {"grab_at": None, "grab_end": None, "released": None,
                               "grab_err": None, "skipped": False, "line": None}
                pad.use["line"] = line
                self._flush_use(pad)
            if mirror:
                return
            self._last_chord = (now, pad.key)
            self.last_chord_key = pad.key
            if order is None:
                log.info("controller shortcut on %s", pad.name)
            if self.on_chord:
                self._call(self.on_chord)

    # ------------------------------------------------------------- timers
    def next_timeout(self, now: float | None = None) -> float | None:
        """Seconds until :meth:`tick` has work (0 = now), None = no timer needed."""
        now = self.clock() if now is None else now
        due = []
        for pad in self.pads.values():
            if pad.repeat_at is not None and pad.dir is not None and not pad.dir_blocked:
                due.append(pad.repeat_at)
            if pad.chord_since is not None and not pad.chord_latched:
                due.append(pad.chord_since + self.hold)
            if pad.grab_wait_since is not None and not pad.grabbed:
                due.append(pad.grab_wait_since + self.grab_wait_s)
                if pad.grab_poll_at is not None:
                    due.append(pad.grab_poll_at)
            if pad.mod_check_at is not None:
                due.append(pad.mod_check_at)
        if self._want_grab and self.watchdog_s:
            due.append(self._activity + self.watchdog_s)
        if self._grab_lost_pending:
            due.append(now)
        if self._next_rescan is not None:
            due.append(self._next_rescan)
        if not due:
            return None
        return max(0.0, min(due) - now)

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        try:
            for pad in list(self.pads.values()):
                if (self.navigate and pad.dir and not pad.dir_blocked
                        and pad.repeat_at is not None and now >= pad.repeat_at - 1e-9):
                    self._emit(pad, pad.dir, True, now)
                    pad.repeat_at += self.repeat_interval
                    if pad.repeat_at <= now:
                        pad.repeat_at = now + self.repeat_interval
                if pad.chord_since is not None:
                    self._update_chord(pad, now)
                if pad.grab_wait_since is not None:
                    self._check_pending_grab(pad, now)
                if pad.mod_check_at is not None and now >= pad.mod_check_at - 1e-9:
                    self._check_modifier(pad, now)
            with self._lock:
                if self._want_grab and self.watchdog_s and now - self._activity >= self.watchdog_s:
                    log.warning("controller grab watchdog: nothing for %.0f s; giving the controllers back",
                                self.watchdog_s)
                    self._release_grab()
                    self._grab_lost_pending = True
            if self._grab_lost_pending:
                self._grab_lost_pending = False
                if self.on_grab_lost:
                    self._safe(self.on_grab_lost)
            if self._next_rescan is not None and now >= self._next_rescan:
                self.rescan()
        except Exception:
            log.exception("controller timer failed; releasing the controllers")
            self.ungrab()

    # ------------------------------------------------------------- hotplug
    def _on_inotify(self) -> None:
        paths, gone = set(), set()
        for name, mask in self._inotify.read():
            if not (name.startswith("event") and name[5:].isdigit()):
                continue
            path = os.path.join(INPUT_DIR, name)
            if mask & (_Inotify.IN_DELETE | _Inotify.IN_MOVED_FROM):
                gone.add(path)
                paths.discard(path)
            else:
                paths.add(path)
                gone.discard(path)
        for path in gone:
            self._skip.pop(path, None)
            if path in self.pads:
                self._drop(path, notify=True)
        if paths:
            self.rescan([p for p in paths if _sysfs_has_key(p, BTN_SOUTH) is not False])

    # ------------------------------------------------------------- grab
    @property
    def grabbing(self) -> bool:
        """True while a grab is wanted (between :meth:`grab` and :meth:`ungrab`)."""
        return self._want_grab

    def is_grabbed(self, key) -> bool:
        """Is the pad ``key`` (e.g. :attr:`last_chord_key`) held exclusively by us?"""
        pad = self.pads.get(key)
        return bool(pad and pad.grabbed)

    def grab_state(self) -> str:
        """"exclusive" (every pad grabbed), "partial", "shared" (none) or "off"."""
        if not self._want_grab:
            return "off"
        pads = list(self.pads.values())
        n = sum(p.grabbed for p in pads)
        if pads and n == len(pads):
            return "exclusive"
        return "partial" if n else "shared"

    def grab(self, now: float | None = None) -> None:
        """Take every pad exclusively (deferred per pad until nothing is held on it)."""
        now = self.clock() if now is None else now
        with self._lock:
            self._want_grab = True
            self._activity = now
            self._grab_lost_pending = False
            for pad in self.pads.values():
                pad.grab_failed = False
                self._try_grab(pad, now)
        self._start_watchdog_thread()
        self._changed(timer_only=True)

    def renew(self, now: float | None = None) -> None:
        """Tell the watchdog the grab is still wanted (e.g. the bar is busy)."""
        self._activity = self.clock() if now is None else now

    def ungrab(self) -> None:
        """Give every pad back (the bar's grab and the shortcut's modifier hold)."""
        with self._lock:
            self._want_grab = False
            self._release_grab()
        self._wd_stop.set()

    def _release_grab(self) -> None:
        self._want_grab = False
        for pad in list(self.pads.values()):
            pad.grab_wait_since = None
            pad.busy_since = pad.grab_poll_at = None
            pad.grab_failed = False
            self._mod_release(pad)
            if pad.grabbed:
                pad.grabbed = False
                try:
                    pad.dev.ungrab()
                except Exception as e:  # already gone / never ours
                    log.debug("ungrab %s: %s", pad.key, e)

    def _idle(self, pad: _Pad) -> bool:
        if any(c in pad.names for c in pad.held) or pad.hat != [0, 0]:
            return False
        for code, v in pad.axes.items():
            if code in (ABS_Z, ABS_RZ):
                if v > 0.5:
                    return False
            elif abs(v) > 0.5:
                return False
        return True

    def _check_pending_grab(self, pad: _Pad, now: float) -> None:
        if self._want_grab and not pad.grabbed and not pad.grab_failed:
            with self._lock:
                self._try_grab(pad, now)

    def _try_grab(self, pad: _Pad, now: float) -> None:
        if not self._want_grab or pad.grabbed or pad.grab_failed or pad.fd is None:
            return
        if pad.mod_grabbed:                 # already ours (the shortcut's hold): keep it
            pad.mod_grabbed, pad.mod_since = False, None
            pad.grab_wait_since = pad.busy_since = pad.grab_poll_at = None
            pad.grabbed = True
            return
        waited = pad.grab_wait_since is not None
        if waited:
            self._resync_blind(pad)     # another program's grab may have hidden the releases
        if not self._idle(pad):
            if pad.grab_wait_since is None:
                pad.grab_wait_since = now
            if now - pad.grab_wait_since < self.grab_wait_s - 1e-9:
                pad.grab_poll_at = now + GRAB_POLL_S
                return
        try:
            pad.dev.grab()
        except OSError as e:
            if e.errno == errno.EBUSY:
                # the daemon may still hold it for the shortcut: it lets go within
                # CHORD_GRAB_S, so keep trying a little before sharing the pad
                if pad.busy_since is None:
                    pad.busy_since = now
                if now - pad.busy_since < self.grab_busy_s - 1e-9:
                    if pad.grab_wait_since is None:
                        pad.grab_wait_since = now
                    pad.grab_poll_at = now + GRAB_POLL_S
                    return
            pad.grab_wait_since = pad.busy_since = pad.grab_poll_at = None
            pad.grab_failed = True
            _warn_once(("grab", pad.key, pad.name),
                       "can't take %s exclusively (%s; another program holds it?); "
                       "the game will also see presses while the bar is open", pad.name, e.strerror or e)
            return
        pad.grab_wait_since = pad.busy_since = pad.grab_poll_at = None
        pad.grabbed = True
        if waited:
            self._resync_blind(pad)     # what happened while someone else held it

    def _wd_period(self) -> float:
        period = max(0.5, min(5.0, self.watchdog_s / 4)) if self.watchdog_s else 5.0
        if self.chord_grab:
            period = min(period, max(0.25, self.chord_grab_s / 4))
        return period

    def _watchdog_check(self, period: float) -> bool:
        """The thread's check (under the lock); False once there is nothing to watch."""
        now = self.clock()
        alive = False
        if self._want_grab and self.watchdog_s:
            if now - self._activity >= self.watchdog_s + period:
                log.warning("controller grab watchdog (thread): releasing the controllers")
                self._release_grab()
                self._grab_lost_pending = True
            else:
                alive = True
        try:
            pads = list(self.pads.values())
        except RuntimeError:  # changed under us by the loop thread: look again later
            return True
        for pad in pads:
            if pad.mod_grabbed:
                if now - (pad.mod_since or now) >= self.chord_grab_s + period:
                    log.warning("controller shortcut watchdog (thread): giving %s back", pad.name)
                    self._mod_release(pad, why="watchdog")
                else:
                    alive = True
        return alive

    def _start_watchdog_thread(self) -> None:
        if not self._watchdog_thread_on:
            return
        if self._wd_thread is not None and self._wd_thread.is_alive() and not self._wd_stop.is_set():
            return  # still watching this grab
        # (a stopped thread may still be winding down: it keeps its own, set, Event)
        self._wd_stop = threading.Event()
        ref = weakref.ref(self)
        stop = self._wd_stop
        period = self._wd_period()

        def run():  # last line of defence if the event loop itself is stuck
            while not stop.wait(period):
                hub = ref()
                if hub is None:
                    return
                with hub._lock:
                    if not hub._watchdog_check(period):
                        return
                del hub

        self._wd_thread = threading.Thread(target=run, name="momento-gamepad-watchdog", daemon=True)
        self._wd_thread.start()

    # ------------------------------------------------------------- plumbing
    def _call(self, fn, *args) -> None:
        """Run a callback; if it raises, log it and give the controllers back."""
        try:
            fn(*args)
        except Exception:
            log.exception("controller callback failed; releasing the controllers")
            self.ungrab()

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("controller callback failed")

    def _changed(self, timer_only: bool = False) -> None:
        for fn in list(self._listeners):
            try:
                fn(timer_only)
            except Exception:
                log.exception("controller loop adapter failed")

    # ------------------------------------------------------------- adapters
    def attach_glib(self):
        """Drive this hub from the default GLib main context. Returns a handle with ``detach()``."""
        return _GlibAdapter(self)

    def attach_qt(self, parent=None):
        """Drive this hub from the running Qt event loop. Returns a handle with ``detach()``."""
        return _QtAdapter(self, parent)


class _GlibAdapter:
    def __init__(self, hub: Gamepads):
        from gi.repository import GLib

        self.GLib = GLib
        self.hub = hub
        self.watches: dict[int, int] = {}
        self.timer = None
        self.timer_due = None
        hub._listeners.append(self.sync)
        self.sync()

    def sync(self, timer_only: bool = False) -> None:
        GLib = self.GLib
        if not timer_only:
            fds = set(self.hub.fds())
            for fd in [f for f in self.watches if f not in fds]:
                GLib.source_remove(self.watches.pop(fd))
            for fd in fds - set(self.watches):
                self.watches[fd] = GLib.io_add_watch(
                    fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_io)
        self._schedule()

    def _schedule(self) -> None:
        wait = self.hub.next_timeout()
        if wait is None:
            return
        due = time.monotonic() + wait
        if self.timer is not None and self.timer_due is not None and self.timer_due <= due + 0.001:
            return  # the pending timer fires early enough; tick() reschedules
        if self.timer is not None:
            self.GLib.source_remove(self.timer)
        self.timer_due = due
        self.timer = self.GLib.timeout_add(max(1, int(wait * 1000 + 0.999)), self._on_timer)

    def _on_io(self, fd, _cond) -> bool:
        self.hub.process(fd)
        keep = fd in self.hub.fds()
        if not keep:
            self.watches.pop(fd, None)
        self.sync()
        return keep

    def _on_timer(self) -> bool:
        self.timer = self.timer_due = None
        self.hub.tick()
        self.sync()
        return False

    def detach(self) -> None:
        if self.sync in self.hub._listeners:
            self.hub._listeners.remove(self.sync)
        for src in self.watches.values():
            self.GLib.source_remove(src)
        self.watches.clear()
        if self.timer is not None:
            self.GLib.source_remove(self.timer)
            self.timer = None


class _QtAdapter:
    def __init__(self, hub: Gamepads, parent=None):
        from PySide6.QtCore import QObject, QSocketNotifier, QTimer

        self.QSocketNotifier = QSocketNotifier
        self.hub = hub
        self.holder = QObject(parent)
        self.notifiers: dict[int, object] = {}
        self.timer = QTimer(self.holder)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._on_timer)
        self.timer_due = None
        hub._listeners.append(self.sync)
        self.sync()

    def sync(self, timer_only: bool = False) -> None:
        if not timer_only:
            fds = set(self.hub.fds())
            for fd in [f for f in self.notifiers if f not in fds]:
                n = self.notifiers.pop(fd)
                n.setEnabled(False)
                n.deleteLater()
            for fd in fds - set(self.notifiers):
                n = self.QSocketNotifier(fd, self.QSocketNotifier.Read, self.holder)
                n.activated.connect(lambda *_, fd=fd: self._on_io(fd))
                self.notifiers[fd] = n
        self._schedule()

    def _schedule(self) -> None:
        wait = self.hub.next_timeout()
        if wait is None:
            return
        due = time.monotonic() + wait
        if self.timer.isActive() and self.timer_due is not None and self.timer_due <= due + 0.001:
            return
        self.timer_due = due
        self.timer.start(max(0, int(wait * 1000 + 0.999)))

    def _on_io(self, fd) -> None:
        self.hub.process(fd)
        self.sync()

    def _on_timer(self) -> None:
        self.timer_due = None
        self.hub.tick()
        self.sync()

    def detach(self) -> None:
        if self.sync in self.hub._listeners:
            self.hub._listeners.remove(self.sync)
        for n in self.notifiers.values():
            n.setEnabled(False)
            n.deleteLater()
        self.notifiers.clear()
        self.timer.stop()


# ------------------------------------------------------------ test double
class _AbsInfo:
    __slots__ = ("value", "min", "max", "fuzz", "flat", "resolution")

    def __init__(self, value=0, min=-32768, max=32767):  # noqa: A002 - evdev's field names
        self.value, self.min, self.max = value, min, max
        self.fuzz = self.flat = self.resolution = 0


class _Info:
    def __init__(self, vendor=0, product=0, bustype=3, version=1):
        self.vendor, self.product, self.bustype, self.version = vendor, product, bustype, version


class _Event:
    __slots__ = ("type", "code", "value", "sec", "usec")

    def __init__(self, t, c, v, ts=None):
        self.type, self.code, self.value = t, c, v
        if ts is None:
            self.sec = self.usec = None
        else:
            self.sec = int(ts)
            self.usec = round((ts - self.sec) * 1e6)


class FakeDevice:
    """A stand-in for ``evdev.InputDevice``: a real pipe fd, scripted events.

    ``push(type, code, value)`` queues an event (``syn=True`` adds SYN_REPORT)
    and makes the fd readable, so it also works under a real event loop.
    ``grab_error`` (an errno) makes :meth:`grab` fail like a device someone else
    already holds; ``unplug()`` makes the next read fail with ENODEV.
    ``mask_honoured`` drops pushed events the event mask filters out, and
    ``grabbed_by_other`` (with ``grab_error=EBUSY``) hides all of them, as the
    kernel does; the device state (``held``, ``axes``) changes either way.
    """

    XBOX_KEYS = (BTN_SOUTH, BTN_EAST, BTN_X, BTN_Y, BTN_TL, BTN_TR, BTN_SELECT, BTN_START,
                 BTN_MODE, BTN_THUMBL, BTN_THUMBR)

    def __init__(self, name="Fake pad", path=None, keys=XBOX_KEYS + tuple(BTN_TRIGGER_HAPPY1 + i for i in range(8)),
                 axes=None, vendor=0x045e, driver="", grab_error=None):
        self.name = name
        self.path = path or f"/fake/{name}"
        self.phys = self.uniq = ""
        self.info = _Info(vendor=vendor)
        self.driver = driver
        self.keys = list(keys)
        if axes is None:
            axes = {ABS_X: _AbsInfo(0), ABS_Y: _AbsInfo(0), ABS_RX: _AbsInfo(0), ABS_RY: _AbsInfo(0),
                    ABS_Z: _AbsInfo(0, 0, 255), ABS_RZ: _AbsInfo(0, 0, 255),
                    ABS_HAT0X: _AbsInfo(0, -1, 1), ABS_HAT0Y: _AbsInfo(0, -1, 1)}
        self.axes = axes
        self.held: set[int] = set()
        self.grab_error = grab_error
        self.grabbed_by_other = False
        self.mask_honoured = False
        self.grabbed = False
        self.grab_calls = 0
        self.ungrab_calls = 0
        self.mask = "all"
        self.mask_codes = None
        self.closed = False
        self._queue: list[_Event] = []
        self._unplugged = False
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self.fd = self._r

    # evdev.InputDevice surface
    def fileno(self) -> int:
        return self._r

    def capabilities(self, verbose=False, absinfo=True):
        caps = {EV_SYN: [0], EV_KEY: list(self.keys)}
        if self.axes:
            caps[EV_ABS] = [(c, i) if absinfo else c for c, i in self.axes.items()]
        return caps

    def absinfo(self, code):
        return self.axes[code]

    def active_keys(self):
        return sorted(self.held)

    def read(self):
        if self._unplugged:
            raise OSError(errno.ENODEV, "No such device")
        try:
            os.read(self._r, 4096)
        except BlockingIOError:
            pass
        if not self._queue:
            raise BlockingIOError(errno.EAGAIN, "no events")
        out, self._queue = self._queue, []
        return iter(out)

    def grab(self):
        self.grab_calls += 1
        if self.grab_error is not None:
            raise OSError(self.grab_error, os.strerror(self.grab_error))
        if self.grabbed:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
        self.grabbed = True

    def ungrab(self):
        self.ungrab_calls += 1
        if not self.grabbed:
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        self.grabbed = False

    def set_clock_monotonic(self) -> bool:
        """Like EVIOCSCLOCKID: ``push(..., ts=)`` times are on the hub's (fake) clock."""
        return True

    def set_event_mask(self, types, codes=None):
        self.mask = "all" if types is None else tuple(sorted(types))
        self.mask_codes = {t: tuple(sorted(c)) for t, c in codes.items()} if codes else None

    def masked(self, t, code) -> bool:
        """Would the kernel keep this event from us (EVIOCSMASK)? EV_SYN never is."""
        if t == EV_SYN or self.mask == "all":
            return False
        if t not in self.mask:
            return True
        return bool(self.mask_codes and t in self.mask_codes and code not in self.mask_codes[t])

    def close(self):
        if not self.closed:
            self.closed = True
            self.grabbed = False  # the kernel drops a grab with the fd
            for fd in (self._r, self._w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    # scripting
    def push(self, t, code, value, syn=True, ts=None):
        if t == EV_KEY:
            (self.held.add if value else self.held.discard)(code)
        elif t == EV_ABS and code in self.axes:
            self.axes[code].value = value
        if self.grabbed_by_other or (self.mask_honoured and self.masked(t, code)):
            return                          # the kernel state changed, but we see nothing
        self._queue.append(_Event(t, code, value, ts))
        if syn:
            self._queue.append(_Event(EV_SYN, SYN_REPORT, 0, ts))
        if not self.closed:
            os.write(self._w, b"x")

    def unplug(self):
        self._unplugged = True
        if not self.closed:
            os.write(self._w, b"x")


# ------------------------------------------------------------ probe / CLI
def probe(paths: Iterable[str] | None = None) -> list[dict]:
    """Read-only description of the connected pads (no grab)."""
    out = []
    if _sandboxed():
        return out
    for path in (paths if paths is not None else list_candidates()):
        try:
            dev = open_device(path)
        except OSError as e:
            out.append({"path": path, "error": e.strerror or str(e)})
            continue
        if dev is None:
            continue
        try:
            caps = dev.capabilities(absinfo=True)
            keys = set(caps.get(EV_KEY, ()))
            layout = layout_for(dev.info.vendor, getattr(dev, "driver", ""), dev.name)
            symbols = symbols_for(dev.info.vendor, getattr(dev, "driver", ""), dev.name)
            pad = _Pad(None, path, layout=layout,
                       has_hat=any(c in (ABS_HAT0X, ABS_HAT0Y) for c, _ in caps.get(EV_ABS, ()))
                       or BTN_DPAD_UP in keys)
            out.append({
                "path": path, "name": dev.name, "vendor": f"{dev.info.vendor:04x}",
                "product": f"{dev.info.product:04x}", "driver": getattr(dev, "driver", ""),
                "layout": layout, "symbols": symbols,
                "buttons": sorted({pad.names[c] for c in keys if c in pad.names}, key=BUTTON_NAMES.index),
                "axes": {c: (i.min, i.max) for c, i in caps.get(EV_ABS, ())},
            })
        finally:
            dev.close()
    return out


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python3 -m momento.gamepad",
                                 description="List game controllers Momento can use; --watch prints their input.")
    ap.add_argument("--watch", action="store_true", help="print actions and the shortcut (read only, no grab)")
    ap.add_argument("--chord", default="+".join(DEFAULT_CHORD), help="shortcut buttons, e.g. mode+dpad_down")
    ap.add_argument("--hold-ms", type=int, default=DEFAULT_HOLD_MS,
                    help="how long to hold the shortcut, in ms (default: 0, fire on press)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not available():
        print("python-evdev is not installed")
        return 1
    pads = probe()
    if not pads:
        print("no readable game controllers")
    for p in pads:
        if "error" in p:
            print(f"{p['path']}: can't read it ({p['error']}); usually one that InputPlumber or "
                  "Steam has taken over, which Momento then sees through its virtual controller")
            continue
        print(f"{p['path']}: {p['name']} [{p['vendor']}:{p['product']} {p['driver'] or '-'}, "
              f"{p['layout']} layout, {p['symbols']} symbols]\n  buttons: {' '.join(p['buttons'])}")
    if not args.watch:
        return 0
    from gi.repository import GLib

    hub = Gamepads(on_action=lambda a, r: print("action", a, "(repeat)" if r else ""),
                   on_button=lambda n, down: print("button", n, "down" if down else "up"),
                   on_chord=lambda: print("SHORTCUT"), chord=normalize_chord(args.chord), hold_ms=args.hold_ms)
    hub.start()
    hub.attach_glib()
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        hub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
