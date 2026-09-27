"""Bar sounds: what tools/make_sounds.py makes, momento.sfx, and the sound each bar action plays.

    python3 -m unittest tests.test_sounds

Nothing is played out loud: the bar gets a ``sfx.Sounds`` whose backend only records
the names (``overlay.SOUND_FACTORY``), and under the test sandbox the real backend is
never even created. The daemon is a fake (tests/test_overlay_offscreen.py).
"""

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import array
import math
import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("QT_WAYLAND_SHELL_INTEGRATION", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_sounds  # noqa: E402

from momento import config, sfx  # noqa: E402


def samples(path) -> tuple[array.array, tuple]:
    with wave.open(str(path), "rb") as w:
        fmt = (w.getframerate(), w.getsampwidth(), w.getnchannels())
        data = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder != "little":
        data.byteswap()
    return data, fmt


def dbfs(peak: int) -> float:
    return 20 * math.log10(peak / 32767)


class Recorder:
    """A backend that plays nothing: it keeps the names (and checks it got real PCM)."""

    def __init__(self):
        self.names = []

    def play(self, name, pcm):
        assert isinstance(pcm, bytes) and len(pcm) > 1000, name
        self.names.append(name)


class Clock:
    """Monotonic seconds; each call moves on by ``step`` (1 s: no move is ever too soon)."""

    def __init__(self, step=1.0):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


# --------------------------------------------------------------------------
# the sounds themselves
# --------------------------------------------------------------------------

class GeneratedSounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ["MOMENTO_TEST_SANDBOX"])
        cls.out = Path(cls._tmp.name) / "sounds"
        cls.reel = Path(cls._tmp.name) / "reel.wav"
        with mock.patch("sys.stdout"):
            make_sounds.main(["--out", str(cls.out), "--reel", str(cls.reel)])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_sound_the_bar_plays(self):
        self.assertEqual(tuple(make_sounds.SOUNDS), sfx.NAMES)
        self.assertEqual(sorted(p.stem for p in self.out.glob("*.wav")), sorted(sfx.NAMES))
        self.assertEqual(sorted(p.stem for p in sfx.SOUND_DIR.glob("*.wav")), sorted(sfx.NAMES))

    def test_format_length_and_level(self):
        total = 0
        for name in sfx.NAMES:
            data, fmt = samples(self.out / f"{name}.wav")
            self.assertEqual(fmt, (48_000, 2, 1), name)                   # 48 kHz, 16-bit, mono
            ms = len(data) / 48
            self.assertTrue(30 <= ms <= 400, f"{name}: {ms:.0f} ms")
            peak = dbfs(max(abs(v) for v in data))
            want = -26.0 if name == "move" else -18.0
            self.assertAlmostEqual(peak, want, delta=0.2, msg=name)
            self.assertEqual((data[0], data[-1]), (0, 0), name)          # no click at either end
            self.assertLess(abs(sum(data) / len(data)), 8, name)          # no DC offset
            total += (self.out / f"{name}.wav").stat().st_size
        self.assertLess(total, 400_000)                                   # tiny, all of them

    def test_committed_files_are_the_scripts(self):
        """momento/sounds/ is what the script makes (re-run it after changing a sound)."""
        for name in sfx.NAMES:
            new, _ = samples(self.out / f"{name}.wav")
            old, _ = samples(sfx.SOUND_DIR / f"{name}.wav")
            self.assertEqual(len(new), len(old), name)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(new, old)), 2, name)   # libm rounding

    def test_deterministic_and_the_reel(self):
        a = make_sounds.render("open")
        self.assertEqual(a, make_sounds.render("open"))
        data, fmt = samples(self.reel)
        self.assertEqual(fmt, (48_000, 2, 1))
        gaps = len(sfx.NAMES) * make_sounds.REEL_GAP_S * 48_000
        parts = sum(len(samples(self.out / f"{n}.wav")[0]) for n in sfx.NAMES)
        self.assertEqual(len(data), parts + round(gaps))

    def test_candidates_length_and_level(self):
        """The record / pause / stop candidates: 120-350 ms, -18 dBFS, clean ends."""
        self.assertEqual({n: sorted(v) for n, v in make_sounds.VARIANTS.items()},
                         {n: ["A", "B", "C"] for n in ("record", "pause", "stop")})
        for name, options in make_sounds.VARIANTS.items():
            for v in options:
                data = make_sounds.render(name, v)
                label = f"{name} {v}"
                ms = len(data) / 48
                self.assertTrue(120 <= ms <= 350, f"{label}: {ms:.0f} ms")
                peak = max(abs(s) for s in data)
                self.assertAlmostEqual(dbfs(peak), -18.0, delta=0.2, msg=label)
                self.assertEqual((data[0], data[-1]), (0, 0), label)
                self.assertLess(max(abs(s) for s in data[:48]), peak * 0.25, label)   # a soft attack, no click
                self.assertLess(abs(sum(data) / len(data)), 8, label)
                self.assertEqual(data, make_sounds.render(name, v), label)          # deterministic

    def test_use_a_candidate_and_the_preview(self):
        out = Path(self._tmp.name) / "use"
        with mock.patch("sys.stdout"):
            make_sounds.main(["--out", str(out), "--use", "pause=B"])
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                make_sounds.main(["--out", str(out), "--use", "pause=Z"])
        self.assertEqual(samples(out / "pause.wav")[0], make_sounds.render("pause", "B"))
        self.assertEqual(samples(out / "record.wav")[0], make_sounds.render("record"))   # the rest: defaults
        paths = make_sounds.preview(Path(self._tmp.name) / "pv" / "sfx")
        self.assertEqual([p.name for p in paths], ["sfx-options.wav", "sfx-options.txt", "sfx-sequence-A.wav",
                                                   "sfx-sequence-B.wav", "sfx-sequence-C.wav"])
        self.assertEqual(samples(paths[2])[1], (48_000, 2, 1))
        self.assertIn("stop C", paths[1].read_text())

    def test_read_wav(self):
        pcm = sfx.read_wav(self.out / "move.wav")
        self.assertEqual(len(pcm), len(samples(self.out / "move.wav")[0]) * 2)
        bad = Path(self._tmp.name) / "stereo.wav"
        with wave.open(str(bad), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44_100)
            w.writeframes(b"\0" * 400)
        with self.assertRaises(ValueError):
            sfx.read_wav(bad)


# --------------------------------------------------------------------------
# momento.sfx.Sounds
# --------------------------------------------------------------------------

class SoundsPlayer(unittest.TestCase):
    def make(self, step=1.0, **kw):
        self.rec = Recorder()
        self.clock = Clock(step)
        return sfx.Sounds(backend=lambda: self.rec, sync=True, clock=self.clock, **kw)

    def test_plays_by_name(self):
        s = self.make()
        for name in sfx.NAMES:
            self.assertTrue(s.play(name))
        self.assertEqual(self.rec.names, list(sfx.NAMES))
        self.assertFalse(s.play("fanfare"))                               # not one of ours

    def test_move_is_rate_limited(self):
        s = self.make(step=0.0)
        self.assertTrue(s.play("move"))
        self.assertFalse(s.play("move"))                                  # held direction: dropped
        self.assertTrue(s.play("select"))                                 # other sounds aren't
        self.clock.t += sfx.MOVE_GAP_S - 0.005
        self.assertFalse(s.play("move"))
        self.clock.t += 0.01
        self.assertTrue(s.play("move"))
        self.assertEqual(self.rec.names, ["move", "select", "move"])
        self.assertEqual(sfx.MOVE_GAP_S, 0.040)

    def test_off_means_silence(self):
        s = self.make()
        s.enabled = False
        for name in sfx.NAMES:
            self.assertFalse(s.play(name))
        self.assertEqual(self.rec.names, [])
        s.enabled = True
        s.play("open")
        self.assertEqual(self.rec.names, ["open"])

    def test_no_audio_is_silent_and_said_once(self):
        def missing():
            raise OSError("libpulse-simple.so.0: cannot open shared object file")
        s = sfx.Sounds(backend=missing, sync=True)
        self.assertTrue(s.available)                                      # not known before the first try
        with self.assertLogs("momento.sfx", "INFO") as logs:
            s.play("open")
            s.play("move")
            s.warm()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("bar sounds off", logs.output[0])
        self.assertFalse(s.available)

    def test_missing_sounds_are_silent(self):
        rec = Recorder()
        s = sfx.Sounds(backend=lambda: rec, sync=True, sound_dir=Path("/nonexistent/momento-sounds"))
        with self.assertLogs("momento.sfx", "INFO"):
            s.play("open")
        self.assertEqual(rec.names, [])

    def test_a_failing_stream_goes_quiet_then_retries(self):
        s = self.make(step=0.0)
        calls = []

        def broken(name, pcm):
            calls.append(name)
            raise OSError("no sound output (Connection refused)")
        self.rec.play = broken
        with self.assertLogs("momento.sfx", "INFO"):
            s.play("open")
        s.play("select")                                                  # quiet meanwhile
        self.assertEqual(calls, ["open"])
        self.clock.t += sfx.RETRY_S + 0.1
        s.play("select")
        self.assertEqual(calls, ["open", "select"])

    def test_free_drops_the_sounds(self):
        s = self.make()
        s.play("open")
        self.assertIsNotNone(s._pcm)
        s.free()
        self.assertIsNone(s._pcm)
        self.assertIsNone(s._out)
        s.play("close")                                                   # loads again if asked
        self.assertEqual(self.rec.names, ["open", "close"])

    def test_threads_never_block_the_caller(self):
        gate = threading.Event()
        rec = Recorder()
        slow_names = []

        def slow(name, pcm):
            slow_names.append(name)
            gate.wait(5)
        rec.play = slow
        s = sfx.Sounds(backend=lambda: rec)
        t = time.monotonic()
        handed = [s.play(n) for n in ("open", "select", "save", "error")]
        self.assertLess(time.monotonic() - t, 0.2)
        self.assertEqual(handed, [True] * sfx.MAX_STREAMS + [False])      # a stuck server: dropped
        self.assertTrue(s.busy())
        self.assertFalse(s.wait(0.05))
        gate.set()
        self.assertTrue(s.wait(2))
        self.assertFalse(s.busy())
        self.assertEqual(sorted(slow_names), sorted(["open", "select", "save"]))

    def test_the_sandbox_never_reaches_the_sound_server(self):
        with mock.patch.object(sfx, "PulseOut") as real:                 # the default backend
            s = sfx.Sounds(sync=True)
            s.play("open")
            s.warm()
        real.assert_not_called()
        self.assertFalse(s.available)

    def test_pulse_out_errors(self):
        out = sfx.PulseOut.__new__(sfx.PulseOut)

        class Lib:
            freed = []

            def pa_simple_new(self, *a):
                return None

            def pa_strerror(self, code):
                return b"Connection refused"
        out.lib = Lib()
        out.spec = out.attr = None
        with mock.patch("ctypes.byref", lambda x: x):
            with self.assertRaises(OSError) as cm:
                out.play("open", b"\0" * 10)
        self.assertIn("no sound output", str(cm.exception))

    def test_priority(self):
        self.assertEqual(sfx.first(["move", "select"]), "select")
        self.assertEqual(sfx.first(["select", "error"]), "error")
        self.assertEqual(sfx.first(["move"]), "move")
        self.assertIsNone(sfx.first([]))
        self.assertEqual(set(sfx.PRIORITY), set(sfx.NAMES))


# --------------------------------------------------------------------------
# the bar
# --------------------------------------------------------------------------

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from momento import ipc, overlay  # noqa: E402

try:
    from tests import test_overlay_offscreen as base  # noqa: E402  (the module: its tests don't rerun here)
except ImportError:
    import test_overlay_offscreen as base  # noqa: E402
FakeDaemon, pump = base.FakeDaemon, base.pump


class BarSounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        cls._orig = ipc.request
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ["MOMENTO_TEST_SANDBOX"])
        cls._last = overlay.LAST_FILE
        overlay.LAST_FILE = Path(cls._tmp.name) / "overlay.last"

    @classmethod
    def tearDownClass(cls):
        ipc.request = cls._orig
        overlay.LAST_FILE = cls._last
        cls._tmp.cleanup()

    wait_for = base.OverlayOffscreen.wait_for

    def setUp(self):
        self.rec = Recorder()
        self.clock = Clock()
        self.made = []

        def factory():
            s = sfx.Sounds(backend=lambda: self.rec, sync=True, clock=self.clock)
            self.made.append(s)
            return s
        self.addCleanup(setattr, overlay, "SOUND_FACTORY", overlay.SOUND_FACTORY)
        overlay.SOUND_FACTORY = factory
        cfg = config.default_path()
        self.assertTrue(str(cfg).startswith(os.environ["MOMENTO_TEST_SANDBOX"]))
        self.addCleanup(lambda: cfg.unlink(missing_ok=True))

    def make(self, daemon=None, **kw):
        """A resident bar, shown the way the hotkey shows it."""
        self.daemon = daemon or FakeDaemon(True, **kw)
        ipc.request = self.daemon.request
        Bar, fetch_status = overlay._build([])
        bar = Bar()
        bar.resident = True
        bar.apply_status(fetch_status(timeout=1.0))
        self.app.installEventFilter(bar)
        self.addCleanup(bar.close)
        self.addCleanup(self.app.removeEventFilter, bar)
        self.addCleanup(bar.dismiss)
        bar.present()
        pump(self.app, 0.1)
        return bar

    def key(self, k):
        target = self.app.focusWidget() or self.app.activeWindow()
        self.app.sendEvent(target, QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))

    def heard(self):
        names, self.rec.names = list(self.rec.names), []
        return names

    # ---- open / close
    def test_open_on_show_close_on_esc(self):
        bar = self.make()
        self.assertEqual(self.heard(), ["open"])
        self.key(Qt.Key_Escape)
        self.assertFalse(bar.isVisible())
        self.assertEqual(self.heard(), ["close"])
        bar.present()
        self.assertEqual(self.heard(), ["open"])
        self.assertEqual(len(self.made), 1)                               # one player, made once

    def test_automatic_hides_are_silent(self):
        bar = self.make()
        self.heard()
        bar.on_idle()                                                     # the idle timeout
        self.assertFalse(bar.isVisible())
        bar.present()
        self.heard()
        bar.on_leave()                                                    # the pointer left
        self.assertEqual(self.heard(), [])

    def test_toggle_over_the_control_socket(self):
        daemon = FakeDaemon(True)
        real = base.REAL_REQUEST

        def route(msg, timeout=120, path=None, **kw):
            return real(msg, timeout=timeout, path=path) if path is not None else daemon.request(msg)
        ipc.request = route
        sock = config.RUNTIME_DIR / "overlay-sounds-test.sock"
        bar, server = overlay.start_resident(self.app, False, path=sock)
        self.addCleanup(bar.dismiss)
        self.addCleanup(self.app.removeEventFilter, bar)
        self.addCleanup(server.close)
        runner = base.ResidentBar("test_toggle_show_hide")
        runner.app, runner.sock = self.app, sock
        self.assertTrue(runner.send("toggle")["visible"])
        self.assertFalse(runner.send("toggle")["visible"])
        self.assertTrue(runner.send("show")["visible"])
        self.assertFalse(runner.send("hide")["visible"])                  # not the user's key: silent
        self.assertEqual(self.heard(), ["open", "close", "open"])

    # ---- moving
    def test_move_with_keys_and_the_controller(self):
        bar = self.make()
        self.heard()
        first = self.app.focusWidget()
        self.key(Qt.Key_Right)
        self.assertIsNot(self.app.focusWidget(), first)
        bar.on_pad_action("left")                                         # the D-pad: the same sound
        self.assertIs(self.app.focusWidget(), first)
        self.assertEqual(self.heard(), ["move", "move"])

    def test_holding_a_direction_is_rate_limited(self):
        bar = self.make()
        self.heard()
        self.clock.step = 0.0
        for _ in range(5):
            bar.on_pad_action("right", repeat=True)
        self.assertEqual(self.heard(), ["move"])
        self.clock.t += sfx.MOVE_GAP_S + 0.001
        bar.on_pad_action("right", repeat=True)
        self.assertEqual(self.heard(), ["move"])

    # ---- saving
    def test_save_sounds_on_success_not_on_press(self):
        bar = self.make()
        self.heard()
        self.key(Qt.Key_Return)                                           # the focused length
        self.assertEqual(self.heard(), ["select"])
        self.wait_for(lambda: bar.done)
        self.assertEqual(self.heard(), ["save"])

    def test_a_failed_save_is_an_error(self):
        bar = self.make(fail=True)
        self.heard()
        self.key(Qt.Key_1)
        self.wait_for(lambda: bar.done)
        self.assertEqual(self.heard(), ["select", "error"])

    def test_greyed_length_is_refused(self):
        bar = self.make(extra={"max_seconds": 900})                       # 30m and 60m greyed
        self.heard()
        self.assertFalse(bar.options[7].isEnabled())
        self.key(Qt.Key_8)
        self.assertEqual(self.heard(), ["error"])
        QTest.mouseClick(bar.options[6], Qt.LeftButton)
        self.assertEqual(self.heard(), ["error"])
        self.assertEqual(self.daemon.saves, [])

    # ---- play / pause / stop / screenshot
    def test_pause_play_and_stop(self):
        bar = self.make()
        self.heard()
        self.key(Qt.Key_P)
        self.assertEqual(self.heard(), ["pause"])
        self.wait_for(lambda: bar.view == "paused" and not bar.control_busy)
        self.key(Qt.Key_P)
        self.assertEqual(self.heard(), ["record"])
        self.wait_for(lambda: not bar.control_busy)
        bar.controls[0]["stop"].setFocus()
        self.key(Qt.Key_Return)                                           # the stop question
        self.assertEqual((bar.mode, self.heard()), ("confirm", ["select"]))
        self.key(Qt.Key_Escape)                                           # Cancel
        self.assertEqual((bar.mode, self.heard()), ("clip", ["select"]))
        bar.ask_stop()
        self.heard()
        self.key(Qt.Key_Left)                                             # Cancel -> Stop
        self.assertEqual(self.heard(), ["move"])
        self.key(Qt.Key_Return)
        self.assertEqual(self.heard(), ["stop"])
        self.wait_for(lambda: not bar.isVisible(), timeout=3)            # it hides by itself...
        self.assertEqual(self.heard(), [])                               # ...without a close sound

    def test_play_refused_for_storage(self):
        bar = self.make(extra={"state": "no_storage", "recording": False, "error": base.LOW_ERROR},
                        storage=base.LOW)
        self.heard()
        bar.toggle_pause()
        self.assertEqual(self.heard(), ["error"])

    def test_start_from_off(self):
        bar = self.make(FakeDaemon(False))
        self.heard()
        with mock.patch.object(overlay, "start_daemon", side_effect=OSError("no systemd")):
            bar.toggle_pause()
            self.assertEqual(self.heard(), ["record"])
            self.wait_for(lambda: not bar.control_busy)
        self.assertEqual(self.heard(), ["error"])

    def test_screenshot(self):
        bar = self.make()
        self.heard()
        bar.controls[0]["shot"].click()
        self.assertFalse(bar.isVisible())
        self.wait_for(lambda: self.rec.names)
        self.assertEqual(self.heard(), ["shot"])                          # heard with the bar gone

    # ---- settings
    def open_settings(self, bar):
        self.key(Qt.Key_S)
        self.wait_for(lambda: bar.mode == "settings")
        self.assertEqual(self.heard(), ["select"])

    def test_settings_rows_and_values(self):
        bar = self.make()
        self.heard()
        self.open_settings(bar)
        self.key(Qt.Key_Down)
        self.assertEqual(self.heard(), ["move"])
        row = next(r for r in bar.visible_rows() if r.isAncestorOf(self.app.focusWidget()))
        before = row.value
        self.key(Qt.Key_Left if row.idx == len(row.values) - 1 else Qt.Key_Right)   # a value chosen
        self.assertNotEqual(row.value, before)
        self.assertEqual(self.heard(), ["select"])
        for _ in range(len(row.values) + 1):
            self.key(Qt.Key_Right)
        self.heard()
        self.key(Qt.Key_Right)                                            # at the end: nothing happens
        self.assertEqual(self.heard(), [])
        self.key(Qt.Key_PageDown)                                         # next tab
        self.assertEqual(self.heard(), ["move"])
        self.key(Qt.Key_Escape)                                           # Back
        self.assertEqual((bar.mode, self.heard()), ("clip", ["select"]))

    def test_sounds_row_applies_at_once(self):
        bar = self.make()
        self.heard()
        self.open_settings(bar)
        bar.switch_tab(bar.tab_names.index("Audio"), "row")
        row = bar.row("sounds")
        self.assertEqual((row.value, row.values), ("on", ["on", "off"]))
        self.assertEqual(overlay.ROW_TITLES["sounds"], "Menu sounds")
        row.focus()
        self.heard()
        self.key(Qt.Key_Right)                                            # Off: silent at once
        self.assertEqual((row.value, self.heard()), ("off", []))
        self.key(Qt.Key_Up)
        self.assertEqual(self.heard(), [])
        row.focus()
        self.key(Qt.Key_Left)                                             # On again: heard at once
        self.assertEqual((row.value, self.heard()), ("on", ["select"]))
        self.key(Qt.Key_Right)
        self.key(Qt.Key_Escape)                                           # Back: the saved On again
        self.assertEqual((bar.mode, self.heard()), ("clip", ["select"]))

    def test_apply_sounds_off(self):
        bar = self.make(configure_reply={"ok": True, "changed": {"sounds": "off"}, "restarted": False,
                                         "paused": False})
        self.heard()
        self.open_settings(bar)
        bar.switch_tab(bar.tab_names.index("Audio"), "row")
        bar.row("sounds").select(1)
        bar.apply_settings()
        self.wait_for(lambda: bar.apply_state == "done")
        self.assertEqual(self.daemon.configures, [{"sounds": "off"}])
        self.assertFalse(bar.sounds_on)
        bar.close_settings()
        self.key(Qt.Key_Right)
        self.key(Qt.Key_Escape)
        self.assertEqual(self.heard(), [])

    def test_off_in_the_config_file_means_silence(self):
        config.default_path().parent.mkdir(parents=True, exist_ok=True)
        config.default_path().write_text("[ui]\nsounds = false\n")
        bar = self.make()
        self.key(Qt.Key_Right)
        self.key(Qt.Key_P)
        self.key(Qt.Key_Escape)
        self.assertEqual(self.heard(), [])
        self.assertFalse(bar.sounds_on)
        config.default_path().write_text("[ui]\nsounds = true\n")         # read again on every show
        bar.present()
        self.assertEqual(self.heard(), ["open"])

    def test_apply_refused_for_space(self):
        bar = self.make(free=1e9)
        self.heard()
        self.open_settings(bar)
        bar.apply_btn.setEnabled(False)                                   # the choice doesn't fit
        bar.apply_settings()
        self.assertEqual(self.heard(), ["error"])

    # ---- robustness
    def test_no_audio_never_breaks_the_bar(self):
        def missing():
            raise OSError("libpulse-simple.so.0: cannot open shared object file")
        overlay.SOUND_FACTORY = lambda: sfx.Sounds(backend=missing, sync=True)
        with self.assertLogs("momento.sfx", "INFO") as logs:
            bar = self.make()
            self.key(Qt.Key_Right)
            self.key(Qt.Key_P)
        self.assertEqual(len([m for m in logs.output if "bar sounds off" in m]), 1)
        self.wait_for(lambda: self.daemon.controls == ["pause"])          # the bar went on as usual
        bar.dismiss()

        def broken():
            raise RuntimeError("no player")
        overlay.SOUND_FACTORY = broken
        with self.assertLogs("momento.overlay", "ERROR"):
            bar2 = self.make()
        self.assertIsNone(bar2.sounds)
        self.key(Qt.Key_Right)                                            # silent, still works
        self.assertTrue(bar2.isVisible())

    def test_freed_when_the_bar_recycles(self):
        bar = self.make()
        s = bar.sounds
        self.assertIsNotNone(s._pcm)
        exits = []
        bar.request_exit = exits.append
        bar.recycle = True                                                # the gallery was used
        bar.dismiss()
        pump(self.app, 0.05)
        self.assertEqual(exits, [overlay.BAR_RECYCLE_EXIT])
        self.assertIsNone(bar.sounds)
        self.assertIsNone(s._pcm)                                         # the sounds are gone
        self.assertIsNone(s._out)

    def test_one_input_one_sound(self):
        bar = self.make()
        self.heard()
        bar.with_sounds(lambda: (bar.sound("select"), bar.sound("move"), bar.sound("error")))
        self.assertEqual(self.heard(), ["error"])


if __name__ == "__main__":
    unittest.main()
