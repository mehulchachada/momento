"""The clip bar's UI sounds: short, soft ticks and tones (``momento/sounds/*.wav``).

Made by ``tools/make_sounds.py`` (synthesized, no samples): 48 kHz, 16-bit, mono.

Playback goes through libpulse-simple (ctypes), which PulseAudio and PipeWire's
pulse server both provide. Not QSoundEffect: any QtMultimedia audio object loads
Qt's FFmpeg media plugin, and with it the GPU's video decode driver. Measured on
the ROG Ally (Qt 6.11): +68 MB PSS / +82 MB RSS and a 130-460 ms stall on the GUI
thread the first time, for a bar that is about 66 MB in all. libpulse-simple costs
about 2.4 MB, and nothing runs on the GUI thread: ``play()`` hands the sound to a
short-lived daemon thread (open a stream, write, drain, close: ~15 ms before the
first sample, no stream left open between sounds).

Nothing here may break the bar: without libpulse, a sound server or the WAVs the
bar is simply silent, logged once at INFO.

**Recording.** Momento records the desktop's sound from the default output's
monitor (``pipewiresrc`` with ``stream.capture.sink=true``, or ``pulsesrc`` on
``@DEFAULT_MONITOR@``, see ``pipeline.Recorder._audio_chain``). A sink's monitor
carries the sink's mix, after every stream is added in: PipeWire and PulseAudio
have no property that leaves one playback stream out of it (``media.role``,
``application.name`` only steer routing and policy, the monitor ports mirror the
mixed input). Leaving them out would mean recording every other application's
stream separately, a different capture design. So the sounds can be heard in a
clip that covers the moment they played; they are kept short and soft for that
(peaks at -18 dBFS, ``move`` at -26 dBFS). A save's own sound plays after the
save, past the clip's end.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)

SOUND_DIR = Path(__file__).resolve().parent / "sounds"
NAMES = ("move", "select", "open", "close", "gallery_open", "gallery_close", "save", "shot",
         "record", "pause", "stop", "error", "delete")
RATE = 48_000
MOVE_GAP_S = 0.040      # holding a direction: at most one tick this often
MAX_STREAMS = 3         # sounds playing at once; more are dropped (a stuck sound server)
RETRY_S = 5.0           # after a failed stream: silent this long, then try again
LATENCY_MS = 20         # the stream's target buffer
EXIT_WAIT_S = 0.5       # a closing bar lets a sound finish, this long at most
# When one input causes several sounds (a value chosen, and focus moving with it),
# the first of these wins. "move" is only played when nothing else is.
PRIORITY = ("error", "stop", "save", "delete", "shot", "record", "pause", "gallery_open",
            "gallery_close", "open", "close", "select", "move")


def read_wav(path) -> bytes:
    """The PCM of one of our WAVs; ValueError unless 48 kHz 16-bit mono."""
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, 2, 1):
            raise ValueError(f"{path}: not 48 kHz 16-bit mono")
        return w.readframes(w.getnframes())


def first(names) -> str | None:
    """The sound to play when one input asked for several (see PRIORITY)."""
    names = [n for n in names if n in PRIORITY]
    return min(names, key=PRIORITY.index) if names else None


class _SampleSpec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]


class _BufferAttr(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in ("maxlength", "tlength", "prebuf", "minreq", "fragsize")]


class PulseOut:
    """One sound = one playback stream ("Momento" / "Clip bar sound" in the mixer),
    opened, written, drained and closed by the calling thread (blocking)."""

    PA_STREAM_PLAYBACK = 1
    PA_SAMPLE_S16LE = 3
    DEFAULT = 0xFFFFFFFF   # (uint32_t) -1: the server's choice

    def __init__(self):
        name = ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0"
        lib = ctypes.CDLL(name)                       # OSError when missing
        p, i, err = ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        lib.pa_simple_new.restype = p
        lib.pa_simple_new.argtypes = [ctypes.c_char_p, ctypes.c_char_p, i, ctypes.c_char_p,
                                      ctypes.c_char_p, ctypes.POINTER(_SampleSpec), p,
                                      ctypes.POINTER(_BufferAttr), err]
        lib.pa_simple_write.argtypes = [p, ctypes.c_char_p, ctypes.c_size_t, err]
        lib.pa_simple_drain.argtypes = [p, err]
        lib.pa_simple_free.argtypes = [p]
        self.lib = lib
        self.spec = _SampleSpec(self.PA_SAMPLE_S16LE, RATE, 1)
        d = self.DEFAULT
        self.attr = _BufferAttr(d, RATE * 2 * LATENCY_MS // 1000, d, d, d)

    def _why(self, code: int) -> str:
        try:
            f = self.lib.pa_strerror
            f.restype, f.argtypes = ctypes.c_char_p, [ctypes.c_int]
            return (f(code) or b"").decode(errors="replace") or f"error {code}"
        except (AttributeError, OSError):
            return f"error {code}"

    def play(self, name: str, pcm: bytes) -> None:
        e = ctypes.c_int(0)
        s = self.lib.pa_simple_new(None, b"Momento", self.PA_STREAM_PLAYBACK, None, b"Clip bar sound",
                                   ctypes.byref(self.spec), None, ctypes.byref(self.attr), ctypes.byref(e))
        if not s:
            raise OSError(f"no sound output ({self._why(e.value)})")
        try:
            if self.lib.pa_simple_write(s, pcm, len(pcm), ctypes.byref(e)) < 0:
                raise OSError(f"cannot play ({self._why(e.value)})")
            self.lib.pa_simple_drain(s, ctypes.byref(e))
        finally:
            self.lib.pa_simple_free(s)


class Sounds:
    """The bar's sounds: loaded on the first play (``warm()`` starts that early),
    played without blocking the caller.

    ``backend``: makes the output, an object with ``play(name, pcm)`` that blocks
    while the sound plays (default: ``PulseOut``). ``sync=True`` plays in the caller's thread
    (tests). ``enabled`` is the Sounds setting: False means silence.
    """

    def __init__(self, backend=None, sync=False, clock=time.monotonic, sound_dir=None):
        self.enabled = True
        self.sync = sync
        self.clock = clock
        self.sound_dir = Path(sound_dir) if sound_dir else SOUND_DIR
        self._make = backend if backend is not None else PulseOut
        self._out = None
        self._pcm = None            # name -> bytes, once loaded
        self._dead = False          # no backend or no sounds: silent for good
        self._quiet_until = 0.0     # a stream failed: silent until then
        self._told = False          # the one INFO line
        self._last_move = None
        self._lock = threading.Lock()
        self._active = 0
        self._idle = threading.Condition(self._lock)
        self.played = 0             # sounds handed to the backend (a statistic)

    # ---- loading
    def _say(self, why: str) -> None:
        if not self._told:
            self._told = True
            log.info("bar sounds off: %s", why)

    def _load(self) -> bool:
        """Backend + PCM, once. Runs in a sound thread (or the caller's, with sync)."""
        if self._pcm is not None:
            return True
        if self._dead:
            return False
        if self._make is PulseOut and os.environ.get("MOMENTO_TEST_SANDBOX"):
            self._dead = True        # tests never reach the real sound server
            return False
        try:
            out = self._make()
            pcm = {n: read_wav(self.sound_dir / f"{n}.wav") for n in NAMES}
        except Exception as e:  # noqa: BLE001 - no libpulse, no WAVs, a bad file: silence
            self._dead = True
            self._say(str(e) or e.__class__.__name__)
            return False
        self._out, self._pcm = out, pcm
        return True

    def warm(self) -> None:
        """Load in the background now, so the first sound doesn't wait for it."""
        if self._pcm is not None or self._dead:
            return
        if self.sync:
            self._load()
            return
        threading.Thread(target=self._warm, name="bar-sound-load", daemon=True).start()

    def _warm(self):
        with self._lock:
            self._load()

    @property
    def available(self) -> bool:
        return not self._dead

    # ---- playing
    def play(self, name: str) -> bool:
        """Play ``name`` unless the sounds are off, unavailable or ``move`` came too soon.
        Returns whether it was handed on (not whether it was heard)."""
        if not self.enabled or self._dead or name not in NAMES:
            return False
        now = self.clock()
        if now < self._quiet_until:
            return False
        if name == "move":
            if self._last_move is not None and now - self._last_move < MOVE_GAP_S:
                return False
            self._last_move = now
        if self.sync:
            self._run(name)
            return True
        with self._lock:
            if self._active >= MAX_STREAMS:
                return False
            self._active += 1
        try:
            threading.Thread(target=self._thread, args=(name,), name="bar-sound", daemon=True).start()
        except RuntimeError:  # can't start a thread (shutting down)
            self._done()
            return False
        return True

    def _thread(self, name):
        try:
            with self._lock:
                ok = self._load()
                out, pcm = self._out, (self._pcm or {}).get(name)
            if ok:
                self._send(out, name, pcm)
        finally:
            self._done()

    def _done(self):
        with self._lock:
            self._active -= 1
            self._idle.notify_all()

    def _run(self, name):
        if self._load():
            self._send(self._out, name, (self._pcm or {}).get(name))

    def _send(self, out, name, pcm):
        if out is None or pcm is None:
            return
        try:
            self.played += 1
            out.play(name, pcm)
        except Exception as e:  # noqa: BLE001 - a gone sound server: quiet for a while
            self._quiet_until = self.clock() + RETRY_S
            self._say(str(e) or e.__class__.__name__)

    def busy(self) -> bool:
        with self._lock:
            return self._active > 0

    def wait(self, timeout: float = EXIT_WAIT_S) -> bool:
        """Let the sounds playing now finish (a bar about to quit), ``timeout`` at most."""
        end = time.monotonic() + timeout
        with self._lock:
            while self._active > 0:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._idle.wait(left)
        return True

    def free(self) -> None:
        """Drop the loaded sounds and the backend (the bar recycles or quits).
        Sounds still playing finish; a later ``play`` loads them again."""
        with self._lock:
            self._pcm = None
            self._out = None
            self._last_move = None
