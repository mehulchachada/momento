"""Controller support (momento/gamepad.py), without real devices.

Everything runs on FakeDevice (a pipe fd with scripted events) and a fake
clock; nothing opens /dev/input or grabs a real controller.
"""

from tests import _sandbox  # noqa: F401  (must come first)

import errno
import importlib.util
import logging
import os
import sys
import time
import unittest
from unittest import mock

from momento import gamepad as g
from momento.gamepad import (ABS_HAT0X, ABS_HAT0Y, ABS_RZ, ABS_X, ABS_Y, BTN_DPAD_DOWN, BTN_EAST,
                             BTN_SELECT, BTN_SOUTH, BTN_START, BTN_THUMBL, BTN_THUMBR, BTN_TL,
                             BTN_TR, BTN_TRIGGER_HAPPY1, BTN_X, BTN_Y, EV_ABS, EV_KEY, EV_SYN,
                             SYN_DROPPED, SYN_REPORT, FakeDevice, Gamepads, _AbsInfo)

PADDLE1, PADDLE3, PADDLE4 = BTN_TRIGGER_HAPPY1 + 4, BTN_TRIGGER_HAPPY1 + 6, BTN_TRIGGER_HAPPY1 + 7


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def dualsense(**kw):
    axes = {ABS_X: _AbsInfo(128, 0, 255), ABS_Y: _AbsInfo(128, 0, 255),
            g.ABS_Z: _AbsInfo(0, 0, 255), ABS_RZ: _AbsInfo(0, 0, 255),
            ABS_HAT0X: _AbsInfo(0, -1, 1), ABS_HAT0Y: _AbsInfo(0, -1, 1)}
    keys = (BTN_SOUTH, BTN_EAST, BTN_X, BTN_Y, BTN_TL, BTN_TR, g.BTN_TL2, g.BTN_TR2,
            BTN_SELECT, BTN_START, g.BTN_MODE, BTN_THUMBL, BTN_THUMBR)
    return FakeDevice(name=kw.pop("name", "DualSense Wireless Controller"), keys=keys, axes=axes,
                      vendor=0x054c, driver="playstation", **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.actions, self.buttons, self.chords, self.lost = [], [], [], []
        self.devs = []

    def tearDown(self):
        for d in self.devs:
            d.close()

    def hub(self, **kw):
        kw.setdefault("clock", self.clock)
        kw.setdefault("chord", ("select", "start"))       # the old default; DpadChord tests the new one
        kw.setdefault("watchdog_thread", False)
        kw.setdefault("hotplug", "off")
        h = Gamepads(on_action=lambda a, r: self.actions.append((a, r)),
                     on_button=lambda n, d: self.buttons.append((n, d)),
                     on_chord=lambda: self.chords.append(self.clock.t),
                     on_grab_lost=lambda: self.lost.append(self.clock.t), **kw)
        self.addCleanup(h.close)
        return h

    def dev(self, *a, **kw):
        d = FakeDevice(*a, **kw)
        self.devs.append(d)
        return d

    def push(self, hub, dev, t, code, value, syn=True):
        dev.push(t, code, value, syn=syn)
        hub.process(dev.fileno())

    def at(self, hub, t):
        self.clock.t = t
        hub.tick()


class Mapping(Base):
    def test_face_buttons_and_bumpers_xbox_layout(self):
        hub = self.hub()
        d = self.dev(name="Microsoft X-Box One Elite 2 pad")
        hub.add_device(d)
        for code in (BTN_SOUTH, BTN_EAST, BTN_Y, BTN_X, BTN_TL, BTN_TR):
            self.push(hub, d, EV_KEY, code, 1)
            self.push(hub, d, EV_KEY, code, 0)
        # xpad: 0x134 (BTN_Y) is the top button, 0x133 (BTN_X) the left one
        self.assertEqual([a for a, _ in self.actions],
                         ["accept", "back", "settings", "pause", "prev_section", "next_section"])
        self.assertEqual(self.buttons[:2], [("south", True), ("south", False)])

    def test_standard_layout_north_west(self):
        hub = self.hub()
        d = dualsense()
        self.devs.append(d)
        hub.add_device(d)
        self.assertEqual(hub.devices()[0]["layout"], "standard")
        self.push(hub, d, EV_KEY, g.BTN_NORTH, 1)   # Triangle
        self.push(hub, d, EV_KEY, g.BTN_WEST, 1)    # Square
        self.assertEqual([a for a, _ in self.actions], ["settings", "pause"])

    def test_layout_detection(self):
        self.assertEqual(g.layout_for(driver="xpad"), "xbox")
        self.assertEqual(g.layout_for(vendor=0x045e), "xbox")
        self.assertEqual(g.layout_for(vendor=0x28de), "xbox")
        self.assertEqual(g.layout_for(name="Generic X-Box pad"), "xbox")
        self.assertEqual(g.layout_for(vendor=0x054c, driver="playstation"), "standard")
        self.assertEqual(g.layout_for(vendor=0x057e, driver="nintendo"), "standard")

    def test_raw_buttons_do_not_navigate(self):
        hub = self.hub(chord=None)
        d = self.dev()
        hub.add_device(d)
        for code in (BTN_SELECT, BTN_START, g.BTN_MODE, BTN_THUMBL, BTN_THUMBR, PADDLE1, PADDLE3):
            self.push(hub, d, EV_KEY, code, 1)
            self.push(hub, d, EV_KEY, code, 0)
        self.assertEqual(self.actions, [])
        self.assertEqual([n for n, down in self.buttons if down],
                         ["select", "start", "mode", "thumbl", "thumbr", "paddle1", "paddle3"])

    def test_kernel_key_repeat_and_unknown_codes_ignored(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_SOUTH, 1)
        self.push(hub, d, EV_KEY, BTN_SOUTH, 2)
        self.push(hub, d, EV_KEY, 0xA7, 1)  # KEY_RECORD
        self.assertEqual(self.actions, [("accept", False)])

    def test_dpad_hat(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, -1)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0X, -1)
        self.push(hub, d, EV_ABS, ABS_HAT0X, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0X, 1)
        self.assertEqual(self.actions, [("up", False), ("down", False), ("left", False), ("right", False)])

    def test_dpad_buttons(self):
        hub = self.hub()
        d = self.dev(keys=FakeDevice.XBOX_KEYS + (BTN_DPAD_DOWN,))
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_DPAD_DOWN, 1)
        self.push(hub, d, EV_KEY, BTN_DPAD_DOWN, 0)
        self.assertEqual(self.actions, [("down", False)])

    def test_trigger_happy_1_4_are_dpad_only_without_hat(self):
        hub = self.hub()
        hatless = self.dev(name="xpad dpad-as-buttons", path="/fake/a",
                           axes={ABS_X: _AbsInfo(0), ABS_Y: _AbsInfo(0)})
        hub.add_device(hatless)
        for code, want in zip(range(BTN_TRIGGER_HAPPY1, BTN_TRIGGER_HAPPY1 + 4),
                              ("left", "right", "up", "down")):
            self.push(hub, hatless, EV_KEY, code, 1)
            self.push(hub, hatless, EV_KEY, code, 0)
            self.assertEqual(self.actions[-1], (want, False))
        self.actions.clear()
        withhat = self.dev(path="/fake/b")
        hub.add_device(withhat)
        self.push(hub, withhat, EV_KEY, BTN_TRIGGER_HAPPY1, 1)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.buttons[-1], ("extra1", True))
        # hid-steam style: a D-pad made of BTN_DPAD_* keys, and HAPPY1-4 for other buttons
        steam = self.dev(path="/fake/c", keys=FakeDevice.XBOX_KEYS + (BTN_DPAD_DOWN, g.BTN_DPAD_UP,
                         BTN_TRIGGER_HAPPY1), axes={ABS_X: _AbsInfo(0), ABS_Y: _AbsInfo(0)})
        hub.add_device(steam)
        self.push(hub, steam, EV_KEY, BTN_TRIGGER_HAPPY1, 1)
        self.assertEqual(self.actions, [])
        self.clock.t += 1                         # (not the same press as the xpad pad's "down")
        self.push(hub, steam, EV_KEY, BTN_DPAD_DOWN, 1)
        self.assertEqual(self.actions, [("down", False)])

    def test_stick_deadzone_xbox_range(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_X, 12000)          # ~0.37: inside the deadzone
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_ABS, ABS_X, 20000)          # ~0.61
        self.assertEqual(self.actions, [("right", False)])
        self.push(hub, d, EV_ABS, ABS_X, 13000)          # ~0.4: hysteresis keeps it
        self.push(hub, d, EV_ABS, ABS_X, 20000)
        self.assertEqual(len(self.actions), 1)
        self.push(hub, d, EV_ABS, ABS_X, 5000)           # released
        self.push(hub, d, EV_ABS, ABS_Y, -30000)         # up
        self.assertEqual(self.actions[-1], ("up", False))

    def test_stick_deadzone_dualsense_range(self):
        hub = self.hub()
        d = dualsense()
        self.devs.append(d)
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_Y, 160)            # 0.25 down: nothing
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_ABS, ABS_Y, 250)            # 0.96 down
        self.assertEqual(self.actions, [("down", False)])

    def test_stick_axes_in_one_report_pick_dominant(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_X, -25000, syn=False)
        self.push(hub, d, EV_ABS, ABS_Y, 30000)
        self.assertEqual(self.actions, [("down", False)])

    def test_hat_wins_over_stick(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_X, 30000)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, -1)
        self.assertEqual(self.actions, [("right", False), ("up", False)])

    def test_navigate_off_masks_to_keys_and_reports_nothing(self):
        hub = self.hub(navigate=False)
        d = self.dev()
        hub.add_device(d)
        self.assertEqual(d.mask, (EV_KEY,))
        self.push(hub, d, EV_KEY, BTN_SOUTH, 1)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, -1)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.buttons, [])
        hub.set_navigate(True)
        self.assertEqual(d.mask, "all")

    def test_same_press_from_two_pads_counts_once(self):
        """Steam's virtual pad mirrors the real one: one press, one action."""
        hub = self.hub()
        a, b = self.dev(path="/fake/real"), self.dev(path="/fake/steam")
        hub.add_device(a)
        hub.add_device(b)
        self.push(hub, a, EV_KEY, BTN_SOUTH, 1)
        self.clock.t += 0.004
        self.push(hub, b, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(self.actions, [("accept", False)])
        self.clock.t += 0.5
        self.push(hub, b, EV_KEY, BTN_SOUTH, 0)
        self.push(hub, b, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(len(self.actions), 2)

    def test_state_held_when_opened_fires_nothing(self):
        hub = self.hub()
        d = self.dev()
        d.held.add(BTN_SOUTH)
        d.axes[ABS_HAT0Y].value = 1
        hub.add_device(d)
        self.at(hub, 101.0)
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.actions, [("down", False)])

    def test_syn_dropped_resyncs(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        d.held.add(BTN_TL)                                  # kernel state changed meanwhile
        d.push(EV_SYN, SYN_DROPPED, 0, syn=False)
        d.push(EV_KEY, BTN_SOUTH, 1, syn=False)             # discarded until SYN_REPORT
        d.push(EV_SYN, SYN_REPORT, 0, syn=False)
        hub.process(d.fileno())
        self.assertEqual(self.actions, [])
        self.assertIn(BTN_TL, hub.pads[d.path].held)


class Symbols(Base):
    """Which buttons' names the hints show: the pad in use's own (✕ ○ □ △ on a PS pad)."""

    def setUp(self):
        super().setUp()
        self.active = []

    def symbols_hub(self):
        return self.hub(on_active=lambda: self.active.append(self.clock.t))

    def test_detection(self):
        self.assertEqual(g.symbols_for(vendor=0x054c, driver="playstation"), "playstation")
        self.assertEqual(g.symbols_for(driver="playstation"), "playstation")
        self.assertEqual(g.symbols_for(vendor=0x054c), "playstation")        # hid-sony, or Bluetooth
        self.assertEqual(g.symbols_for(driver="sony"), "playstation")
        self.assertEqual(g.symbols_for(vendor=0x057e, driver="nintendo"), "nintendo")
        self.assertEqual(g.symbols_for(vendor=0x045e, driver="xpad"), "xbox")
        self.assertEqual(g.symbols_for(vendor=0x28de, name="Steam Deck"), "xbox")
        self.assertEqual(g.symbols_for(), "xbox")                            # anything else

    def test_labels_by_position(self):
        by = {s: [g.button_symbol(b, s) for b in ("south", "east", "west", "north", "tl", "tr", "tl2", "tr2")]
              for s in ("xbox", "playstation", "nintendo")}
        self.assertEqual(by["xbox"], ["A", "B", "X", "Y", "LB", "RB", "LT", "RT"])
        self.assertEqual(by["playstation"], ["✕", "○", "□", "△", "L1", "R1", "L2", "R2"])
        self.assertEqual(by["nintendo"], ["B", "A", "Y", "X", "L", "R", "ZL", "ZR"])
        self.assertEqual(g.button_symbol("south", "unknown"), "A")

    def test_only_pad_is_in_use(self):
        hub = self.symbols_hub()
        self.assertIsNone(hub.active_pad())
        self.assertEqual(hub.symbols(), "xbox")                     # nothing connected
        d = dualsense()
        self.devs.append(d)
        hub.add_device(d)
        self.assertEqual(hub.active_pad().key, d.path)              # the only one, before any press
        self.assertEqual(hub.symbols(), "playstation")
        self.assertEqual(hub.devices()[0]["symbols"], "playstation")
        self.push(hub, d, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(self.active, [])                           # it already was the one in use

    def test_last_pad_pressed_wins_and_switches(self):
        hub = self.symbols_hub()
        ps = dualsense()
        self.devs.append(ps)
        xb = self.dev(name="Microsoft X-Box One Elite 2 pad", path="/fake/xbox")
        hub.add_device(ps)
        hub.add_device(xb)
        self.assertIsNone(hub.active_pad())                         # two, none pressed yet
        self.assertEqual(hub.symbols(), "xbox")                     # they disagree: Xbox letters
        self.push(hub, ps, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual((hub.symbols(), len(self.active)), ("playstation", 1))
        self.push(hub, ps, EV_KEY, BTN_SOUTH, 0)
        self.clock.t += 1
        self.push(hub, xb, EV_KEY, BTN_TL, 1)                       # the other pad picked up
        self.assertEqual((hub.symbols(), len(self.active)), ("xbox", 2))
        self.push(hub, xb, EV_KEY, BTN_TL, 0)
        self.clock.t += 1
        self.push(hub, ps, EV_ABS, ABS_HAT0Y, 1)                    # the D-pad counts too
        self.assertEqual((hub.symbols(), len(self.active)), ("playstation", 3))
        self.push(hub, ps, EV_ABS, ABS_HAT0Y, 0)
        hub.remove_device(ps.path)                                  # unplugged: the one left
        self.assertEqual(hub.symbols(), "xbox")

    def test_mirrored_pad_does_not_take_over(self):
        """Steam's virtual Xbox pad repeats the real pad's presses a moment later."""
        hub = self.symbols_hub()
        ps = dualsense()
        self.devs.append(ps)
        mirror = self.dev(name="Microsoft X-Box 360 pad 0", path="/fake/steam-virtual", vendor=0x28de)
        hub.add_device(ps)
        hub.add_device(mirror)
        self.push(hub, ps, EV_KEY, BTN_SOUTH, 1)
        self.clock.t += 0.004
        self.push(hub, mirror, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(hub.symbols(), "playstation")
        self.assertEqual([a for a, _ in self.actions], ["accept"])   # and the action counts once
        self.clock.t += 0.5
        self.push(hub, mirror, EV_KEY, BTN_SOUTH, 0)                 # releases don't switch
        self.assertEqual(hub.symbols(), "playstation")
        self.push(hub, ps, EV_KEY, BTN_EAST, 1)                      # the real pad keeps it...
        self.clock.t += 0.004
        self.push(hub, mirror, EV_KEY, BTN_EAST, 1)
        self.assertEqual((hub.symbols(), len(self.active)), ("playstation", 1))

    def test_stick_counts_as_use(self):
        hub = self.symbols_hub()
        xb = self.dev(path="/fake/xbox")
        ps = dualsense()
        self.devs.append(ps)
        hub.add_device(xb)
        hub.add_device(ps)
        self.push(hub, ps, EV_ABS, ABS_X, 255)                      # stick right: an action
        self.assertEqual(hub.symbols(), "playstation")


class Triggers(Base):
    def names(self):
        return [a for a, _ in self.actions]

    def test_actions_are_listed(self):
        self.assertIn("left_trigger", g.ACTIONS)
        self.assertIn("right_trigger", g.ACTIONS)

    def test_analog_hysteresis(self):
        hub = self.hub()
        d = self.dev()                                      # xpad-style: ABS_Z/RZ 0..255, no BTN_TL2
        hub.add_device(d)
        self.push(hub, d, EV_ABS, g.ABS_Z, 140)             # 0.55: not yet
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_ABS, g.ABS_Z, 160)             # 0.63: pulled
        self.assertEqual(self.actions, [("left_trigger", False)])
        self.push(hub, d, EV_ABS, g.ABS_Z, 255)
        self.push(hub, d, EV_ABS, g.ABS_Z, 100)             # 0.39: still latched
        self.push(hub, d, EV_ABS, g.ABS_Z, 200)
        self.assertEqual(len(self.actions), 1)
        self.at(hub, 105.0)                                 # no auto-repeat while held
        self.assertEqual(len(self.actions), 1)
        self.push(hub, d, EV_ABS, g.ABS_Z, 70)              # 0.27: re-armed
        self.push(hub, d, EV_ABS, g.ABS_Z, 180)
        self.assertEqual(self.names(), ["left_trigger", "left_trigger"])
        self.push(hub, d, EV_ABS, ABS_RZ, 255)
        self.assertEqual(self.names()[-1], "right_trigger")
        self.assertEqual(len(self.actions), 3)

    def test_digital_trigger_buttons(self):
        hub = self.hub()
        d = self.dev(keys=FakeDevice.XBOX_KEYS + (g.BTN_TL2, g.BTN_TR2), axes={})
        hub.add_device(d)
        self.push(hub, d, EV_KEY, g.BTN_TL2, 1)
        self.push(hub, d, EV_KEY, g.BTN_TL2, 2)             # kernel repeat: ignored
        self.push(hub, d, EV_KEY, g.BTN_TL2, 0)
        self.push(hub, d, EV_KEY, g.BTN_TR2, 1)
        self.push(hub, d, EV_KEY, g.BTN_TR2, 0)
        self.push(hub, d, EV_KEY, g.BTN_TL2, 1)
        self.assertEqual(self.names(), ["left_trigger", "right_trigger", "left_trigger"])
        self.assertIn(("tl2", True), self.buttons)          # raw button still reported

    def test_dualsense_button_and_axis_fire_once(self):
        hub = self.hub()
        d = dualsense()
        self.devs.append(d)
        hub.add_device(d)
        # hid-playstation: the button goes down early in the pull, the axis follows
        d.push(EV_ABS, g.ABS_Z, 20, syn=False)
        d.push(EV_KEY, g.BTN_TL2, 1)
        hub.process(d.fileno())
        for v in (120, 200, 255, 200):
            self.push(hub, d, EV_ABS, g.ABS_Z, v)
        self.assertEqual(self.names(), ["left_trigger"])
        # axis below the re-arm point but the button still down: still one pull
        self.push(hub, d, EV_ABS, g.ABS_Z, 40)
        self.push(hub, d, EV_ABS, g.ABS_Z, 200)
        self.assertEqual(len(self.actions), 1)
        # both let go: re-armed
        d.push(EV_ABS, g.ABS_Z, 0, syn=False)
        d.push(EV_KEY, g.BTN_TL2, 0)
        hub.process(d.fileno())
        d.push(EV_KEY, g.BTN_TL2, 1, syn=False)
        d.push(EV_ABS, g.ABS_Z, 30)
        hub.process(d.fileno())
        self.assertEqual(self.names(), ["left_trigger", "left_trigger"])

    def test_navigate_off_emits_nothing(self):
        hub = self.hub(navigate=False)
        d = dualsense()
        self.devs.append(d)
        hub.add_device(d)
        self.assertEqual(d.mask, (EV_KEY,))
        self.push(hub, d, EV_KEY, g.BTN_TL2, 1)
        self.push(hub, d, EV_ABS, ABS_RZ, 255)              # (masked out on a real device)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.buttons, [])
        # turning navigation on takes the pulled trigger as already fired
        hub.set_navigate(True)
        self.push(hub, d, EV_ABS, ABS_RZ, 250)
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_KEY, g.BTN_TL2, 0)
        self.push(hub, d, EV_ABS, ABS_RZ, 0)
        self.push(hub, d, EV_ABS, ABS_RZ, 255)
        self.assertEqual(self.names(), ["right_trigger"])

    def test_pulled_when_opened_fires_nothing(self):
        hub = self.hub()
        d = dualsense()
        self.devs.append(d)
        d.held.add(g.BTN_TL2)
        hub.add_device(d)
        self.push(hub, d, EV_ABS, g.ABS_Z, 255)
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_KEY, g.BTN_TL2, 0)
        self.push(hub, d, EV_ABS, g.ABS_Z, 0)
        self.push(hub, d, EV_KEY, g.BTN_TL2, 1)
        self.assertEqual(self.names(), ["left_trigger"])

    def test_centred_or_signed_axis_is_not_a_trigger(self):
        hub = self.hub()
        axes = {ABS_X: _AbsInfo(0), ABS_Y: _AbsInfo(0),
                g.ABS_Z: _AbsInfo(128, 0, 255),               # generic HID: right stick X
                ABS_RZ: _AbsInfo(0, -32768, 32767)}           # signed range
        d = self.dev(axes=axes)
        hub.add_device(d)
        for v in (0, 255, 128):
            self.push(hub, d, EV_ABS, g.ABS_Z, v)
        self.push(hub, d, EV_ABS, ABS_RZ, 32767)
        self.assertEqual(self.actions, [])

    def test_mirrored_pads_fire_once(self):
        hub = self.hub()
        a, b = self.dev(path="/fake/real"), self.dev(path="/fake/steam")
        hub.add_device(a)
        hub.add_device(b)
        self.push(hub, a, EV_ABS, g.ABS_Z, 255)
        self.clock.t += 0.004
        self.push(hub, b, EV_ABS, g.ABS_Z, 255)
        self.assertEqual(self.names(), ["left_trigger"])
        self.clock.t += 0.5
        for dev in (a, b):
            self.push(hub, dev, EV_ABS, g.ABS_Z, 0)
        self.push(hub, b, EV_ABS, g.ABS_Z, 255)
        self.assertEqual(self.names(), ["left_trigger", "left_trigger"])

    def test_trigger_counts_as_activity_for_the_watchdog(self):
        hub = self.hub(watchdog_s=10)
        d = self.dev()
        hub.add_device(d)
        hub.grab()
        self.at(hub, 108.0)
        self.push(hub, d, EV_ABS, g.ABS_Z, 255)
        self.at(hub, 115.0)
        self.assertTrue(hub.grabbing)
        self.assertEqual(self.lost, [])


class Repeat(Base):
    def test_initial_delay_then_interval(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)             # t=100
        self.assertEqual(self.actions, [("down", False)])
        self.assertAlmostEqual(hub.next_timeout(), 0.35)
        self.at(hub, 100.349)
        self.assertEqual(len(self.actions), 1)
        self.at(hub, 100.35)
        self.assertEqual(self.actions[-1], ("down", True))
        self.at(hub, 100.439)
        self.assertEqual(len(self.actions), 2)
        self.at(hub, 100.44)
        self.at(hub, 100.53)
        self.assertEqual(self.actions, [("down", False)] + [("down", True)] * 3)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.at(hub, 102.0)
        self.assertEqual(len(self.actions), 4)
        self.assertIsNone(hub.next_timeout())

    def test_late_tick_does_not_burst(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_X, 32000)
        self.at(hub, 103.0)                                 # the loop was busy for 3 s
        self.assertEqual(len(self.actions), 2)
        self.assertAlmostEqual(hub.next_timeout(), 0.09)

    def test_direction_change_restarts_delay(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_HAT0X, 1)
        self.at(hub, 100.3)
        self.push(hub, d, EV_ABS, ABS_HAT0X, -1)
        self.at(hub, 100.5)
        self.assertEqual(self.actions, [("right", False), ("left", False)])
        self.at(hub, 100.65)
        self.assertEqual(self.actions[-1], ("left", True))

    def test_custom_timing(self):
        hub = self.hub(repeat_delay_ms=200, repeat_interval_ms=50)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, -1)
        self.at(hub, 100.2)
        self.at(hub, 100.25)
        self.assertEqual(len(self.actions), 3)


class Chord(Base):
    def test_both_held_fires_once(self):
        hub = self.hub(hold_ms=300)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.clock.t = 100.1
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.assertAlmostEqual(hub.next_timeout(), 0.3)
        self.at(hub, 100.399)
        self.assertEqual(self.chords, [])
        self.at(hub, 100.4)
        self.assertEqual(self.chords, [100.4])
        self.at(hub, 105.0)
        self.assertEqual(len(self.chords), 1)

    def test_released_early_does_not_fire(self):
        hub = self.hub(hold_ms=300)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.clock.t = 100.2
        self.push(hub, d, EV_KEY, BTN_START, 0)
        self.at(hub, 101.0)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, BTN_START, 1)            # the hold restarts from here
        self.at(hub, 101.29)
        self.assertEqual(self.chords, [])
        self.at(hub, 101.3)
        self.assertEqual(self.chords, [101.3])

    def test_rearm_after_release(self):
        hub = self.hub(hold_ms=300)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.at(hub, 100.5)
        self.push(hub, d, EV_KEY, BTN_START, 0)
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.at(hub, 101.0)
        self.assertEqual(self.chords, [100.5, 101.0])

    def test_single_paddle_hold(self):
        hub = self.hub(chord=["left_paddle"], hold_ms=300)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, PADDLE1, 1)               # right paddle: not ours
        self.at(hub, 101.0)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, PADDLE4, 1)               # lower-left paddle counts too
        self.at(hub, 101.3)
        self.assertEqual(self.chords, [101.3])
        self.push(hub, d, EV_KEY, PADDLE4, 0)
        self.push(hub, d, EV_KEY, PADDLE3, 1)
        self.at(hub, 101.6)
        self.assertEqual(len(self.chords), 2)

    def test_held_when_opened_needs_release(self):
        """The bar opens while the chord that opened it is still down: no second fire."""
        hub = self.hub(hold_ms=300)
        d = self.dev()
        d.held |= {BTN_SELECT, BTN_START}
        hub.add_device(d)
        self.at(hub, 102.0)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, BTN_SELECT, 0)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.at(hub, 102.5)
        self.assertEqual(self.chords, [102.5])

    def test_chord_needs_one_pad(self):
        hub = self.hub()
        a, b = self.dev(path="/fake/a"), self.dev(path="/fake/b")
        hub.add_device(a)
        hub.add_device(b)
        self.push(hub, a, EV_KEY, BTN_SELECT, 1)
        self.push(hub, b, EV_KEY, BTN_START, 1)
        self.at(hub, 101.0)
        self.assertEqual(self.chords, [])

    def test_mirrored_pads_fire_once(self):
        hub = self.hub()
        a, b = self.dev(path="/fake/a"), self.dev(path="/fake/b")
        hub.add_device(a)
        hub.add_device(b)
        for d in (a, b):
            self.push(hub, d, EV_KEY, BTN_SELECT, 1)
            self.push(hub, d, EV_KEY, BTN_START, 1)
        self.at(hub, 100.5)
        self.assertEqual(len(self.chords), 1)

    def test_works_with_navigation_off_and_zero_hold(self):
        hub = self.hub(navigate=False, chord=["l3", "r3"], hold_ms=0)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_THUMBL, 1)
        self.push(hub, d, EV_KEY, BTN_THUMBR, 1)
        self.assertEqual(self.chords, [100.0])

    def test_tap_fires_on_press(self):
        """hold_ms = 0 (Open with: Tap): the chord fires the instant both buttons are down."""
        hub = self.hub(hold_ms=300)
        d = self.dev()
        hub.add_device(d)
        hub.set_chord(("select", "start"), 0)             # switched live, same hub
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.assertEqual(self.chords, [])
        self.clock.t = 100.05
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.assertEqual(self.chords, [100.05])            # no tick needed
        self.assertIsNone(hub.next_timeout())              # and no timer left behind
        self.at(hub, 103.0)                                # still held: once
        self.assertEqual(len(self.chords), 1)
        self.push(hub, d, EV_KEY, BTN_START, 0)
        self.clock.t = 103.1
        self.push(hub, d, EV_KEY, BTN_START, 1)            # tapped again
        self.assertEqual(self.chords, [100.05, 103.1])

    def test_tap_held_when_opened_needs_release(self):
        """A bar opened by a tap sees the chord still down: it doesn't close at once."""
        hub = self.hub(hold_ms=0)
        d = self.dev()
        d.held |= {BTN_SELECT, BTN_START}
        hub.add_device(d)
        self.at(hub, 100.1)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, BTN_SELECT, 0)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.assertEqual(self.chords, [100.1])

    def test_default_opens_on_press(self):
        """The default (like [controller] hold_ms) is a tap: no hold, no timer."""
        hub = self.hub()
        self.assertEqual(hub.hold, 0)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.assertEqual(self.chords, [100.0])
        self.assertIsNone(hub.next_timeout())

    def test_disabled(self):
        hub = self.hub(chord=None)
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.at(hub, 110.0)
        self.assertEqual(self.chords, [])
        self.assertIsNone(hub.next_timeout())

    def test_normalize_and_labels(self):
        self.assertEqual(g.normalize_chord(["View", "Menu"]), ("select", "start"))
        self.assertEqual(g.normalize_chord("l3+r3"), ("thumbl", "thumbr"))
        self.assertEqual(g.normalize_chord(["left-paddle"]), ("left_paddle",))
        with self.assertRaises(ValueError):
            g.normalize_chord(["select", "turbo"])
        with self.assertRaises(ValueError):
            g.normalize_chord([])
        for key, label, buttons in g.CHORD_PRESETS:
            self.assertEqual(g.chord_label(buttons), label)
            g.normalize_chord(buttons)
        self.assertEqual(g.chord_label(["mode", "south"]), "Mode + South")
        self.assertEqual(g.DEFAULT_CHORD, ("mode", "dpad_down"))
        self.assertEqual(g.DEFAULT_HOLD_MS, 0)                 # a tap, like [controller] hold_ms
        from momento import config
        self.assertEqual(g.DEFAULT_HOLD_MS, config.DEFAULTS["controller"]["hold_ms"])


class Grab(Base):
    def test_grab_and_ungrab(self):
        hub = self.hub()
        a, b = self.dev(path="/fake/a"), self.dev(path="/fake/b")
        hub.add_device(a)
        hub.add_device(b)
        self.assertEqual(hub.grab_state(), "off")
        hub.grab()
        self.assertTrue(a.grabbed and b.grabbed)
        self.assertEqual(hub.grab_state(), "exclusive")
        hub.ungrab()
        self.assertFalse(a.grabbed or b.grabbed)
        self.assertEqual(hub.grab_state(), "off")
        hub.ungrab()                                         # idempotent
        self.assertEqual(a.ungrab_calls, 1)

    def test_grab_waits_until_buttons_released(self):
        """The chord is still held when the bar opens: grab after release, so the game
        sees the release and nothing stays stuck."""
        hub = self.hub()
        d = self.dev()
        d.held |= {BTN_SELECT, BTN_START}
        hub.add_device(d)
        hub.grab()
        self.assertFalse(d.grabbed)
        self.assertEqual(hub.grab_state(), "shared")
        self.push(hub, d, EV_KEY, BTN_SELECT, 0)
        self.assertFalse(d.grabbed)
        self.clock.t = 100.2
        self.push(hub, d, EV_KEY, BTN_START, 0)
        self.assertTrue(d.grabbed)

    def test_grab_waits_for_stick_to_center(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_X, 30000)
        hub.grab()
        self.assertFalse(d.grabbed)
        self.push(hub, d, EV_ABS, ABS_X, 0)
        self.assertTrue(d.grabbed)

    def test_grab_wait_is_bounded(self):
        hub = self.hub(grab_wait_s=1.0)
        d = self.dev()
        d.held.add(BTN_SOUTH)
        hub.add_device(d)
        hub.grab()
        self.assertAlmostEqual(hub.next_timeout(), g.GRAB_POLL_S)     # re-reads the pad meanwhile
        self.at(hub, 100.5)
        self.at(hub, 100.9)
        self.assertFalse(d.grabbed)
        self.at(hub, 101.0)
        self.assertTrue(d.grabbed)

    def test_grab_failure_falls_back_to_shared_and_logs_once(self):
        hub = self.hub(grab_busy_s=0)                        # (EBUSY retries: DpadChord)
        busy = self.dev(name="Held by Steam", path="/fake/busy", grab_error=errno.EBUSY)
        ok = self.dev(path="/fake/ok")
        hub.add_device(busy)
        hub.add_device(ok)
        g._warned.discard(("grab", "/fake/busy", "Held by Steam"))
        with self.assertLogs("momento.gamepad", logging.WARNING) as cm:
            hub.grab()
            hub.ungrab()
            hub.grab()
        self.assertEqual(sum("exclusively" in m for m in cm.output), 1)
        self.assertEqual(hub.grab_state(), "partial")
        self.assertTrue(ok.grabbed)
        self.assertEqual(busy.grab_calls, 2)                 # retried on every open, logged once
        self.push(hub, busy, EV_KEY, BTN_SOUTH, 1)           # still works, just shared
        self.assertEqual(self.actions[-1], ("accept", False))
        self.assertEqual(busy.grab_calls, 2)                 # no retry storm on input

    def test_watchdog_releases_after_silence(self):
        hub = self.hub(watchdog_s=60)
        d = self.dev()
        hub.add_device(d)
        hub.grab()
        self.at(hub, 130.0)
        self.push(hub, d, EV_KEY, BTN_TL, 1)                 # input keeps it alive
        self.at(hub, 189.0)
        self.assertTrue(d.grabbed)
        self.assertAlmostEqual(hub.next_timeout(), 1.0)
        with self.assertLogs("momento.gamepad", logging.WARNING):
            self.at(hub, 190.0)
        self.assertFalse(d.grabbed)
        self.assertEqual(hub.grab_state(), "off")
        self.assertEqual(self.lost, [190.0])

    def test_renew_keeps_grab(self):
        hub = self.hub(watchdog_s=60)
        d = self.dev()
        hub.add_device(d)
        hub.grab()
        self.clock.t = 150.0
        hub.renew()
        self.at(hub, 200.0)
        self.assertTrue(d.grabbed)

    def test_watchdog_thread_releases_when_loop_is_stuck(self):
        hub = Gamepads(watchdog_s=0.1, hotplug="off")   # real clock, real thread, no tick()
        self.addCleanup(hub.close)
        d = self.dev()
        hub.add_device(d)
        with self.assertLogs("momento.gamepad", logging.WARNING):
            hub.grab()
            deadline = time.monotonic() + 5
            while d.grabbed and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertFalse(d.grabbed)

    def test_hotplugged_pad_is_grabbed_while_open(self):
        hub = self.hub()
        hub.grab()
        d = self.dev()
        hub.add_device(d)
        self.assertTrue(d.grabbed)

    def test_callback_error_releases(self):
        def boom(action, repeat):
            raise RuntimeError("bar broke")

        hub = Gamepads(on_action=boom, clock=self.clock, watchdog_thread=False, hotplug="off")
        self.addCleanup(hub.close)
        d = self.dev()
        hub.add_device(d)
        hub.grab()
        with self.assertLogs("momento.gamepad", logging.ERROR):
            self.push(hub, d, EV_KEY, BTN_SOUTH, 1)
        self.assertFalse(d.grabbed)

    def test_close_and_exit_release(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        hub.grab()
        g._release_all_at_exit()
        self.assertFalse(d.grabbed)
        hub.grab()
        hub.close()
        self.assertFalse(d.grabbed)
        self.assertTrue(d.closed)

    def test_unplugged_while_grabbed(self):
        hub = self.hub()
        d = self.dev()
        hub.add_device(d)
        hub.grab()
        d.unplug()
        hub.process(d.fileno())
        self.assertEqual(hub.devices(), [])
        hub.ungrab()                                         # nothing left to fail on

MODE, DPAD = g.BTN_MODE, (g.BTN_DPAD_UP, g.BTN_DPAD_DOWN, g.BTN_DPAD_LEFT, g.BTN_DPAD_RIGHT)
KEY_MASK, HAT_MASK = (EV_KEY,), (EV_KEY, EV_ABS)


class DpadChord(Base):
    """The default shortcut, PS/Xbox/Home + D-pad Down: hat or BTN_DPAD_*, the mask
    that widens to the hat while mode is held, and the grab that hides the D-pad."""

    def daemon(self, **kw):
        kw.setdefault("chord", g.DEFAULT_CHORD)
        kw.setdefault("navigate", False)
        kw.setdefault("chord_grab", True)
        return self.hub(**kw)

    def pad(self, hub, **kw):
        d = self.dev(**kw)
        d.mask_honoured = True                        # filtered events never arrive, as in the kernel
        hub.add_device(d)
        return d

    def test_default_and_presets(self):
        from momento import config

        self.assertEqual(g.DEFAULT_CHORD, ("mode", "dpad_down"))
        self.assertEqual(tuple(config.DEFAULTS["controller"]["open_chord"]), g.DEFAULT_CHORD)
        self.assertEqual(g.CHORD_PRESETS[0], ("ps_down", "PS / Xbox + Down", ("mode", "dpad_down")))
        self.assertEqual([k for k, _l, _b in g.CHORD_PRESETS],
                         ["ps_down", "view_menu", "left_paddle", "right_paddle", "l3_r3"])
        self.assertEqual(g.chord_label(g.DEFAULT_CHORD), "PS / Xbox + Down")
        for text in ("ps+down", "Xbox + Down", "guide+dpad_down", "home, dpad-down"):
            self.assertEqual(g.normalize_chord(text), ("mode", "dpad_down"), text)
        self.assertEqual(g.normalize_chord("mode+up"), ("mode", "dpad_up"))
        hub = Gamepads(hotplug="off", watchdog_thread=False)
        self.addCleanup(hub.close)
        self.assertEqual((hub.chord, hub.chord_mod, hub.chord_dpad), (("mode", "dpad_down"), ("mode",), True))
        self.assertFalse(hub.chord_grab)                 # only the daemon asks for it

    def test_hat_mode_first(self):
        hub = self.daemon()
        d = self.pad(hub)
        self.assertEqual(d.mask, KEY_MASK)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)          # D-pad alone: masked, never wakes us
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertTrue(d.grabbed)                        # the game sees nothing from here
        self.assertEqual(d.mask, HAT_MASK)
        self.assertEqual(d.mask_codes, {EV_ABS: (ABS_HAT0X, ABS_HAT0Y)})   # the hat, not the sticks
        self.assertEqual(d._queue, [])
        d.push(EV_ABS, ABS_X, 30000)                      # stick: still filtered
        self.assertEqual(d._queue, [])
        self.clock.t = 100.1
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.chords, [100.1])
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.assertTrue(d.grabbed)                        # held until mode is let go
        self.push(hub, d, EV_KEY, MODE, 0)
        self.assertFalse(d.grabbed)
        self.assertEqual(d.mask, KEY_MASK)
        self.assertIsNone(hub.next_timeout())             # no timer left behind
        self.push(hub, d, EV_KEY, MODE, 1)                # again: fires again
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(len(self.chords), 2)

    def test_other_directions_and_buttons_do_not_fire(self):
        hub = self.daemon()
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, -1)          # up
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0X, 1)           # right
        self.push(hub, d, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, MODE, 0)
        self.push(hub, d, EV_KEY, BTN_SELECT, 1)          # View + Menu isn't the shortcut now
        self.push(hub, d, EV_KEY, BTN_START, 1)
        self.assertEqual(self.chords, [])

    def test_hat_down_first_still_counts(self):
        hub = self.daemon()
        d = self.pad(hub)
        d.push(EV_ABS, ABS_HAT0Y, 1)                      # masked: only the device state changes
        self.assertEqual(d._queue, [])
        self.push(hub, d, EV_KEY, MODE, 1)                # the hat is read when the mask widens
        self.assertEqual(self.chords, [100.0])

    def test_btn_dpad_pad(self):
        """Pads that send BTN_DPAD_* (no hat): key events suffice, the mask never widens."""
        hub = self.daemon()
        d = self.pad(hub, keys=FakeDevice.XBOX_KEYS + DPAD, axes={ABS_X: _AbsInfo(0), ABS_Y: _AbsInfo(0)})
        self.push(hub, d, EV_KEY, BTN_DPAD_DOWN, 1)
        self.push(hub, d, EV_KEY, BTN_DPAD_DOWN, 0)
        self.assertEqual(self.chords, [])
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertTrue(d.grabbed)
        self.assertEqual(d.mask, KEY_MASK)
        self.push(hub, d, EV_KEY, BTN_DPAD_DOWN, 1)
        self.assertEqual(self.chords, [100.0])
        self.push(hub, d, EV_KEY, MODE, 0)
        self.assertFalse(d.grabbed)

    def test_xpad_dpad_as_buttons(self):
        hub = self.daemon()
        d = self.pad(hub, axes={ABS_X: _AbsInfo(0), ABS_Y: _AbsInfo(0)})    # HAPPY1-4 = D-pad
        self.push(hub, d, EV_KEY, MODE, 1)
        self.push(hub, d, EV_KEY, BTN_TRIGGER_HAPPY1 + 3, 1)                # down
        self.assertEqual(self.chords, [100.0])

    def test_dpad_down_pressed_first_on_btn_dpad_pad(self):
        hub = self.daemon()
        d = self.pad(hub, keys=FakeDevice.XBOX_KEYS + DPAD, axes={})
        self.push(hub, d, EV_KEY, BTN_DPAD_DOWN, 1)
        self.push(hub, d, EV_KEY, MODE, 1)                 # order doesn't matter
        self.assertEqual(self.chords, [100.0])

    def test_grab_watchdog(self):
        hub = self.daemon()
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertAlmostEqual(hub.next_timeout(), g.CHORD_GRAB_S)
        self.at(hub, 101.9)
        self.assertTrue(d.grabbed)
        with self.assertLogs("momento.gamepad", logging.WARNING):
            self.at(hub, 102.0)
        self.assertFalse(d.grabbed)
        self.assertEqual(d.mask, HAT_MASK)                 # still held: the hat still counts
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.chords, [102.0])
        self.assertEqual(d.grab_calls, 1)                  # not taken again for the same press
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_KEY, MODE, 0)
        self.assertEqual(d.mask, KEY_MASK)

    def test_missed_release_is_noticed(self):
        """Mode let go while someone else held the pad: the 2 s re-read narrows the mask."""
        hub = self.daemon(chord_grab=False)
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertEqual(d.mask, HAT_MASK)
        d.grabbed_by_other = True                          # the bar took it
        d.push(EV_KEY, MODE, 0)                            # unseen
        self.at(hub, 102.0)
        self.assertEqual(d.mask, KEY_MASK)
        d.grabbed_by_other = False
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)            # masked again: a plain D-pad does nothing
        self.assertEqual(self.chords, [])
        self.assertIsNone(hub.next_timeout())

    def test_grab_fails_falls_back_to_shared(self):
        hub = self.daemon()
        d = self.pad(hub, name="Held by Steam", path="/fake/steam", grab_error=errno.EBUSY)
        g._warned.discard(("modgrab", "/fake/steam", "Held by Steam"))
        with self.assertLogs("momento.gamepad", logging.INFO) as cm:
            self.push(hub, d, EV_KEY, MODE, 1)
            self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertTrue(any("can't hold" in m for m in cm.output))
        self.assertEqual(self.chords, [100.0])            # the shortcut still works
        self.assertFalse(d.grabbed)
        self.push(hub, d, EV_KEY, MODE, 0)
        self.assertEqual(d.ungrab_calls, 0)

    def test_no_grab_unless_asked_or_without_dpad(self):
        hub = self.daemon(chord_grab=False)
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertEqual(d.mask, HAT_MASK)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.chords, [100.0])
        self.assertEqual(d.grab_calls, 0)
        hub2 = self.daemon(chord=("select", "start"))       # no D-pad: shared, key-only, as before
        d2 = self.pad(hub2, path="/fake/2")
        self.push(hub2, d2, EV_KEY, BTN_SELECT, 1)
        self.push(hub2, d2, EV_KEY, BTN_START, 1)
        self.assertEqual(len(self.chords), 2)
        self.assertEqual((d2.grab_calls, d2.mask), (0, KEY_MASK))
        self.assertIsNone(hub2.next_timeout())

    def test_chord_change_and_errors_release(self):
        hub = self.daemon()
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertTrue(d.grabbed)
        hub.set_chord(("select", "start"), 0)
        self.assertFalse(d.grabbed)
        self.assertEqual(d.mask, KEY_MASK)
        hub.set_chord(g.DEFAULT_CHORD, 0)
        self.push(hub, d, EV_KEY, MODE, 0)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertTrue(d.grabbed)
        hub.set_chord_grab(False)
        self.assertFalse(d.grabbed)
        hub.set_chord_grab(True)
        self.push(hub, d, EV_KEY, MODE, 0)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertTrue(d.grabbed)
        hub.close()
        self.assertFalse(d.grabbed)

    def test_callback_error_releases(self):
        def boom():
            raise RuntimeError("open_bar broke")

        hub = Gamepads(on_chord=boom, chord=g.DEFAULT_CHORD, navigate=False, chord_grab=True,
                       clock=self.clock, watchdog_thread=False, hotplug="off")
        self.addCleanup(hub.close)
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        self.assertTrue(d.grabbed)
        with self.assertLogs("momento.gamepad", logging.ERROR):
            self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertFalse(d.grabbed)

    def test_unplug_releases(self):
        hub = self.daemon()
        d = self.pad(hub)
        self.push(hub, d, EV_KEY, MODE, 1)
        d.unplug()
        hub.process(d.fileno())
        self.assertEqual(hub.devices(), [])
        self.assertFalse(d.grabbed)

    def test_watchdog_thread_releases_when_loop_is_stuck(self):
        hub = Gamepads(chord=g.DEFAULT_CHORD, navigate=False, chord_grab=True, chord_grab_s=0.1,
                       hotplug="off")                        # real clock, real thread, no tick()
        self.addCleanup(hub.close)
        d = self.dev()
        hub.add_device(d)
        with self.assertLogs("momento.gamepad", logging.WARNING):
            d.push(EV_KEY, MODE, 1)
            hub.process(d.fileno())
            self.assertTrue(d.grabbed)
            deadline = time.monotonic() + 5
            while d.grabbed and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertFalse(d.grabbed)

    def test_bar_dpad_does_nothing_while_mode_held(self):
        """In the open bar, mode + Down closes it; the Down doesn't also move the focus."""
        hub = self.hub(chord=g.DEFAULT_CHORD)
        d = self.dev()
        hub.add_device(d)
        self.assertEqual(d.mask, "all")
        self.push(hub, d, EV_KEY, MODE, 1)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.chords, [100.0])
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0X, 1)             # any direction, while mode is down
        self.push(hub, d, EV_KEY, MODE, 0)                  # still held after mode: stays quiet
        self.at(hub, 101.0)
        self.assertEqual(self.actions, [])
        self.push(hub, d, EV_ABS, ABS_HAT0X, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.actions, [("down", False)])
        self.assertEqual(d.grab_calls, 0)                   # the bar hub never grabs for the chord

    def test_bar_opened_by_the_chord_needs_release(self):
        hub = self.hub(chord=g.DEFAULT_CHORD)
        d = self.dev()
        d.held.add(MODE)
        d.axes[ABS_HAT0Y].value = 1
        hub.add_device(d)
        self.push(hub, d, EV_ABS, ABS_HAT0X, 1)
        self.assertEqual((self.chords, self.actions), ([], []))
        self.push(hub, d, EV_ABS, ABS_HAT0X, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(self.chords, [100.0])

    def test_hand_off_to_the_bar(self):
        """The daemon holds the pad (EBUSY) while mode is down: the bar keeps trying,
        takes it within 50 ms of the release, and doesn't keep a stale "mode held"."""
        hub = self.hub(chord=g.DEFAULT_CHORD)
        d = self.dev(grab_error=errno.EBUSY)
        d.grabbed_by_other = True                           # the daemon's hold
        d.held.add(MODE)
        d.axes[ABS_HAT0Y].value = 1
        hub.add_device(d)
        hub.grab()
        self.assertFalse(d.grabbed)
        self.assertAlmostEqual(hub.next_timeout(), g.GRAB_POLL_S)
        d.push(EV_ABS, ABS_HAT0Y, 0)                        # released, unseen
        self.at(hub, 100.05)
        self.assertEqual(hub.pads[d.path].hat, [0, 0])      # re-read from the kernel
        self.assertEqual(d.grab_calls, 0)                   # mode still down: wait
        for t in (100.1, 100.5, 101.0, 101.5):              # mode held longer than grab_wait_s
            self.at(hub, t)
        self.assertGreaterEqual(d.grab_calls, 2)            # tried: EBUSY, and again
        self.assertEqual(hub.grab_state(), "shared")
        self.assertFalse(hub.pads[d.path].grab_failed)      # still trying
        d.push(EV_KEY, MODE, 0)                             # unseen
        d.grab_error, d.grabbed_by_other = None, False      # the daemon lets go
        self.at(hub, 101.55)
        self.assertTrue(d.grabbed)
        self.assertEqual(hub.grab_state(), "exclusive")
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)             # a plain Down: navigates, doesn't close
        self.assertEqual(self.chords, [])
        self.assertEqual(self.actions, [("down", False)])
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 0)
        self.push(hub, d, EV_KEY, MODE, 1)                  # mode + Down: closes
        self.push(hub, d, EV_ABS, ABS_HAT0Y, 1)
        self.assertEqual(len(self.chords), 1)

    def test_ebusy_for_good_falls_back_to_shared(self):
        hub = self.hub(chord=g.DEFAULT_CHORD, grab_busy_s=2.5)
        d = self.dev(name="Held for good", path="/fake/held", grab_error=errno.EBUSY)
        hub.add_device(d)
        g._warned.discard(("grab", "/fake/held", "Held for good"))
        hub.grab()
        self.at(hub, 102.4)
        self.assertFalse(hub.pads[d.path].grab_failed)
        with self.assertLogs("momento.gamepad", logging.WARNING):
            self.at(hub, 102.5)
        self.assertTrue(hub.pads[d.path].grab_failed)
        self.assertEqual(hub.grab_state(), "shared")
        calls = d.grab_calls
        self.at(hub, 103.0)
        self.assertEqual(d.grab_calls, calls)                # no more tries
        self.push(hub, d, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(self.actions[-1], ("accept", False))


class Hotplug(Base):
    def setUp(self):
        super().setUp()
        self.nodes = {}                                      # path -> FakeDevice | None | OSError
        self.opened = []

    def lister(self):
        return list(self.nodes)

    def opener(self, path):
        self.opened.append(path)
        what = self.nodes[path]
        if isinstance(what, OSError):
            raise what
        return what

    def test_add_and_remove(self):
        changes = []
        hub = self.hub(lister=self.lister, opener=self.opener, hotplug="poll", rescan_s=2.0,
                       on_devices=lambda: changes.append(len(hub.devices())))
        pad = self.dev(path="/dev/input/event23")
        self.nodes = {"/dev/input/event23": pad, "/dev/input/event8": OSError(errno.EACCES, "denied"),
                      "/dev/input/event2": None}
        self.assertTrue(hub.start())
        self.assertEqual([p["key"] for p in hub.devices()], ["/dev/input/event23"])
        self.assertEqual(hub.fds(), [pad.fileno()])
        self.assertAlmostEqual(hub.next_timeout(), 2.0)

        second = dualsense(path="/dev/input/event28")
        self.devs.append(second)
        self.nodes["/dev/input/event28"] = second
        self.at(hub, 101.0)
        self.assertEqual(len(hub.devices()), 1)              # not yet: next poll at 102
        self.at(hub, 102.0)
        self.assertEqual(sorted(p["key"] for p in hub.devices()), ["/dev/input/event23", "/dev/input/event28"])
        self.push(hub, second, EV_KEY, BTN_SOUTH, 1)
        self.assertEqual(self.actions, [("accept", False)])

        del self.nodes["/dev/input/event23"]                 # node gone
        self.at(hub, 104.0)
        self.assertEqual([p["key"] for p in hub.devices()], ["/dev/input/event28"])
        self.assertTrue(pad.closed)
        second.unplug()                                      # read fails with ENODEV
        hub.process(second.fileno())
        self.assertEqual(hub.devices(), [])
        self.assertEqual(hub.fds(), [])
        self.assertEqual(changes, [1, 2, 1, 0])

    def test_manual_devices_survive_rescan(self):
        hub = self.hub(lister=self.lister, opener=self.opener)
        manual = self.dev(path="/fake/manual")
        hub.add_device(manual)
        hub.rescan()
        self.assertEqual(len(hub.devices()), 1)

    def test_unchanged_non_pad_nodes_are_not_reopened(self):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(dir=os.environ["MOMENTO_TEST_SANDBOX"])
        self.addCleanup(tmp.close)
        self.nodes = {tmp.name: None}
        hub = self.hub(lister=self.lister, opener=self.opener)
        hub.rescan()
        hub.rescan()
        self.assertEqual(self.opened, [tmp.name])
        os.chmod(tmp.name, 0o640)                              # ctime changes (like an ACL update)
        os.utime(tmp.name, ns=(1, 1))
        hub.rescan()
        self.assertEqual(len(self.opened), 2)

    def test_listener_notified_of_fd_changes(self):
        seen = []
        hub = self.hub()
        hub._listeners.append(lambda timer_only: seen.append(timer_only))
        d = self.dev()
        hub.add_device(d)
        hub.remove_device(d.path)
        self.assertIn(False, seen)
        self.assertTrue(d.closed)


class WithoutEvdev(unittest.TestCase):
    def test_imports_and_reports_unavailable(self):
        spec = importlib.util.spec_from_file_location("momento._gamepad_noevdev", g.__file__)
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"evdev": None}):
            spec.loader.exec_module(mod)
            self.assertFalse(mod.available())
            self.assertIsNone(mod.open_device("/dev/input/event0"))
            hub = mod.Gamepads(watchdog_thread=False)
            self.assertFalse(hub.start())
            self.assertEqual(hub.fds(), [])
            hub.grab()
            hub.ungrab()
            hub.close()


class Helpers(unittest.TestCase):
    def test_sysfs_bitmap(self):
        pad = "7fdb000000000000 0 0 0 0".split()                    # DualSense
        kbd = "402000007 ff803078f800d001 feffffdfffcfffff fffffffffffffffe".split()
        self.assertTrue(g._bitmap_has(pad, BTN_SOUTH, 64))
        self.assertTrue(g._bitmap_has(pad, BTN_THUMBR, 64))
        self.assertFalse(g._bitmap_has(pad, g.BTN_C, 64))
        self.assertFalse(g._bitmap_has(kbd, BTN_SOUTH, 64))
        self.assertTrue(g._bitmap_has(kbd, 1, 64))                   # KEY_ESC

    def test_event_mask_on_non_evdev_fd_is_harmless(self):
        r, w = os.pipe()
        try:
            self.assertFalse(g.set_event_mask(r, (EV_KEY,)))
        finally:
            os.close(r)
            os.close(w)

    def test_codes_match_evdev(self):
        try:
            from evdev import ecodes
        except ImportError:
            self.skipTest("python-evdev not installed")
        for name in ("BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST", "BTN_TL", "BTN_TR", "BTN_TL2",
                     "BTN_TR2", "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR",
                     "BTN_DPAD_UP", "BTN_DPAD_DOWN", "BTN_DPAD_LEFT", "BTN_DPAD_RIGHT",
                     "BTN_TRIGGER_HAPPY1", "ABS_X", "ABS_Y", "ABS_Z", "ABS_RZ", "ABS_HAT0X", "ABS_HAT0Y",
                     "EV_KEY", "EV_ABS", "EV_SYN", "SYN_REPORT", "SYN_DROPPED"):
            self.assertEqual(getattr(g, name), getattr(ecodes, name), name)
        self.assertEqual(g._EVIOCSMASK, 0x40104593)


def _have(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


@unittest.skipUnless(_have("gi"), "PyGObject not installed")
class GlibLoop(unittest.TestCase):
    def test_actions_and_chord_timer_on_a_glib_loop(self):
        from gi.repository import GLib

        ctx = GLib.MainContext.default()
        got = []
        hub = Gamepads(on_action=lambda a, r: got.append(a), on_chord=lambda: got.append("chord"),
                       chord=("select", "start"), hold_ms=60, hotplug="off", watchdog_thread=False)
        d = FakeDevice()
        hub.add_device(d)
        handle = hub.attach_glib()
        try:
            d.push(EV_KEY, BTN_SOUTH, 1)
            d.push(EV_KEY, BTN_SELECT, 1)
            d.push(EV_KEY, BTN_START, 1)
            deadline = time.monotonic() + 3
            while "chord" not in got and time.monotonic() < deadline:
                ctx.iteration(False)
                time.sleep(0.005)
            self.assertEqual(got, ["accept", "chord"])
            d.unplug()                                        # watch goes away cleanly
            deadline = time.monotonic() + 3
            # the pad leaves hub.devices() a moment before the adapter drops its watch
            while (hub.devices() or handle.watches) and time.monotonic() < deadline:
                ctx.iteration(False)
                time.sleep(0.001)
            self.assertEqual(hub.devices(), [])
            self.assertEqual(handle.watches, {})
        finally:
            handle.detach()
            hub.close()


@unittest.skipUnless(_have("PySide6.QtCore"), "PySide6 not installed")
class QtLoop(unittest.TestCase):
    def test_repeat_on_a_qt_loop(self):
        from PySide6.QtCore import QCoreApplication, QEventLoop

        app = QCoreApplication.instance()
        if app is None:
            # A widgets-capable app (later offscreen bar tests reuse it) that doesn't
            # take over GLib's default context, which other tests run in a thread.
            from PySide6.QtWidgets import QApplication

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            with mock.patch.dict(os.environ, {"QT_NO_GLIB": "1"}):
                app = QApplication(["test"])
        got = []
        hub = Gamepads(on_action=lambda a, r: got.append((a, r)), hotplug="off", watchdog_thread=False,
                       repeat_delay_ms=60, repeat_interval_ms=30)
        d = FakeDevice()
        hub.add_device(d)
        handle = hub.attach_qt()
        try:
            d.push(EV_ABS, ABS_HAT0X, 1)
            deadline = time.monotonic() + 3
            while len(got) < 3 and time.monotonic() < deadline:
                app.processEvents(QEventLoop.AllEvents, 20)
                time.sleep(0.005)
            self.assertEqual(got[:3], [("right", False), ("right", True), ("right", True)])
        finally:
            handle.detach()
            hub.close()


if __name__ == "__main__":
    unittest.main()
