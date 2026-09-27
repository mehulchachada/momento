"""Video formats: H.264, H.265 and AV1. What this machine records in hardware, and what Auto picks.

The setting (``[capture] format``, settings key ``format``) is ``auto`` (the
default), ``h264``, ``h265`` or ``av1``. Saved clips are MP4 in every case.

Detection (``Detector``), done once per machine and cached:

1. Which hardware encoders GStreamer has (``HW_ENCODERS``: VA-API, the legacy
   gstreamer-vaapi H.264/H.265, NVENC, Quick Sync).
2. A tiny real test encode per encoder: 10 frames at 320x240 from
   ``videotestsrc`` into a ``fakesink``, with a timeout, in a child process (so
   a driver that crashes or hangs can't take the daemon along). An element can
   exist while its driver lacks the profile (Fedora's stock Mesa has H.264
   and H.265 VA encode switched off, for one).
3. The result goes to ``<cache dir>/formats.json``, keyed by the encoder
   elements present, the GPUs (render node + PCI vendor:device + kernel driver)
   and the driver version (the VA vendor string, e.g. "Mesa Gallium driver
   26.2.1 for AMD Ryzen Z1 Extreme (radeonsi, phoenix, ...)"; NVIDIA's module
   version). When the key changes (new GPU, driver or GStreamer), the test
   encodes run again.

A format whose start kills the daemon (a driver abort, which no GStreamer
fallback can catch) is caught by ``START_GUARD`` at the next daemon start and
skipped on this GPU and driver until picked again (see "the crash guard").

Software encoders (x264, openh264) are never tested and never make Auto pick a
format: they cost the game far more CPU than any hardware encoder. H.264 still
falls back to them, as the last resort when no hardware H.264 works.

Everything here is pure Python except ``Detector``'s key and probe, which
import GStreamer lazily, so the CLI and the clip bar can import this module
for the labels and the cache without loading it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

FORMATS = ("h264", "h265", "av1")
CHOICES = ("auto",) + FORMATS
DEFAULT = "auto"
LABELS = {"auto": "Auto", "h264": "H.264", "h265": "H.265", "av1": "AV1"}
# Other names people use (`momento set format hevc`, a config file).
ALIASES = {"h.264": "h264", "h-264": "h264", "avc": "h264", "x264": "h264",
           "h.265": "h265", "h-265": "h265", "hevc": "h265", "x265": "h265",
           "av01": "av1", "automatic": "auto"}
# What each format means for the clips: the clip bar's hints, `momento set` help.
HINTS = {
    "h264": "Plays everywhere",
    "h265": "Smaller files. Some older devices can't play it",
    "av1": "Smoothest on newer hardware. Some older devices can't play it",
}

# When the format to record in fails to start (its encoder errors out before the
# first segment), the next one in this order that this machine records is used:
# AV1 -> H.265 -> H.264. Never "up" the list: H.265 falls back to H.264 only.
FALLBACK = ("av1", "h265", "h264")

# Hardware encoders per format, in the order they are tried.
HW_ENCODERS = {
    "h264": ("vah264enc", "vah264lpenc", "vaapih264enc", "nvh264enc", "qsvh264enc"),
    "h265": ("vah265enc", "vah265lpenc", "vaapih265enc", "nvh265enc", "qsvh265enc"),
    # Intel exposes AV1 encode through the low-power entry point only (vaav1lpenc).
    "av1": ("vaav1enc", "vaav1lpenc", "nvav1enc", "qsvav1enc"),
}
# Software encoders, H.264 only: tried after every hardware one, never tested,
# never a reason for Auto to pick a format.
SW_ENCODERS = {"h264": ("x264enc", "openh264enc")}
ENCODER_FORMAT = {name: fmt for table in (HW_ENCODERS, SW_ENCODERS)
                  for fmt, names in table.items() for name in names}

# The segment container per format: (GStreamer muxer, file suffix). MPEG-TS
# carries H.264 and H.265 fine and survives a crash mid-segment. AV1 is not in
# MPEG-TS's standard: GStreamer 1.28's mpegtsmux refuses it unless
# enable-custom-mappings=true, and ffmpeg then reads the stream as data (a save
# with no video). Matroska carries AV1 properly and ffmpeg remuxes it to MP4.
CONTAINERS = {"h264": ("mpegtsmux", ".ts"), "h265": ("mpegtsmux", ".ts"), "av1": ("matroskamux", ".mkv")}
# Muxer properties for the segments. Matroska is written "streamable" (unknown
# sizes, no cues, nothing rewritten at the end), so a save can byte-join the
# segments of one session like MPEG-TS ones (ffmpeg's concatf: reads on past each
# segment's header; with sized segments it stopped after the first one and a 6 s
# save came out 4 s long), and a segment cut off by a crash is as good as a
# finished one.
MUXER_OPTIONS = {"matroskamux": "streamable=true"}

PCI_VENDORS = {"0x1002": "amd", "0x8086": "intel", "0x10de": "nvidia"}

# --- Auto ---------------------------------------------------------------------------
#
# Auto picks, first match wins:
#   1. AUTO_RULES: (GPU vendor, encoder whose test encode passed, format). Add rows
#      here from tester reports (a format measured smoother than H.264 in real games
#      on that hardware, and stable there).
#   2. H.264, when a hardware H.264 encoder works (plays everywhere). With the table
#      empty, this is what Auto records in on every PC that has one.
#   3. Otherwise the first format of FALLBACK with a working hardware encoder (stock
#      Fedora Mesa: no H.264/H.265 VA encode, but AV1 is royalty-free and works).
#   4. H.264 (software, the last resort; nothing else can record at all).
#
# AMD + vaav1enc -> AV1 is switched off for now. On 2026-09-27 a ROG Ally (Z1
# Extreme, Mesa 26.2.1 radeonsi, VCN 4) recording Full screen 1080p60 at Ultra
# (25 Mbps) in AV1 hung the video engine ("ring vcn_unified_0 timeout") 3-5 s after
# every start, and radeonsi then called abort(): the service crashed four times in
# a row. Earlier AV1 runs at Standard (10 Mbps) on the same machine were fine.
# Put the row back once AV1 is proven stable there (see START_GUARD for the crash
# guard that catches a format killing the process).
#
# Why the row existed: only VCN 4.0 and newer encode AV1 (RDNA3 dGPUs, the Phoenix /
# Hawk Point / Strix APUs, Z1 / Z2 in the ROG Ally and Legion Go); VCN 3 has no AV1
# encoder, so vaav1enc's test encode fails there and a passing one is itself the
# "VCN 4 or newer" check. In real-game A/B runs on the ROG Ally (1080p60, 10 Mbps),
# AV1 had fewer frame-time spikes than H.264 in all three runs.
AUTO_RULES = (
    # ("amd", "vaav1enc", "av1"),   # off since the 2026-09-27 VCN hang, see above
)

# --- the crash guard --------------------------------------------------------------
#
# The fallback above only catches GStreamer errors. A driver that aborts (radeonsi
# called abort() after the VCN hang of 2026-09-27) kills the whole process, so
# nothing in it can fall back, and systemd restarts it straight into the same
# crash. START_GUARD leaves a marker (GUARD_NAME, in the cache dir) just before a
# pipeline starts in a GUARDED format, and removes it once that recording has run
# STABLE_SECONDS or on any clean stop (Stop, pause, a settings change, SIGTERM from
# a service restart, logout or shutdown). A marker still there when the daemon
# starts means the last process died while that format was starting: the crash is
# counted in formats.json under the GPU/driver key it ran with, and from
# CRASH_LIMIT crashes on, Auto and a manual pick of that format skip it (the next
# format of FALLBACK records instead) until the key changes (new driver or GPU:
# detection runs again) or the user picks the format again (Detector.clear_crash).
GUARD_NAME = "format-start.json"
GUARDED = ("av1", "h265")   # H.264 is the last resort: there is nothing to fall back to
STABLE_SECONDS = 45         # the 2026-09-27 hangs came 3-5 s after each start
CRASH_LIMIT = 1

PROBE_FRAMES = 10
PROBE_SIZE = (320, 240)
PROBE_TIMEOUT = 8.0         # seconds per encoder (a VA driver's first open can be slow)
# Bump when the probe or the cache layout changes: every cached result is redone.
PROBE_VERSION = 1
CACHE_NAME = "formats.json"


def normalize(value) -> str:
    """"AV1" / "hevc" / "h.264" -> "av1" / "h265" / "h264"; ValueError for anything else."""
    if isinstance(value, bool):
        raise ValueError(f"choose one of: {', '.join(CHOICES)}")
    v = str(value).strip().lower()
    v = ALIASES.get(v, v)
    if v not in CHOICES:
        raise ValueError(f"choose one of: {', '.join(CHOICES)}")
    return v


def configured(capture: dict) -> str:
    """The ``[capture] format`` of a config; anything unknown reads as "auto" (logged)."""
    value = (capture or {}).get("format", DEFAULT)
    try:
        return normalize(value)
    except ValueError:
        log.warning("[capture] format = %r is not one of %s; using auto", value, ", ".join(CHOICES))
        return DEFAULT


def format_of(encoder: str | None) -> str:
    """The format an encoder element writes ("vaav1enc" -> "av1"); unknown ones count as H.264."""
    return ENCODER_FORMAT.get(str(encoder or ""), "h264")


def container(fmt: str) -> tuple[str, str]:
    """(muxer, suffix) of the ring-buffer segments for a format."""
    return CONTAINERS.get(fmt, CONTAINERS["h264"])


def muxer_description(muxer: str) -> str:
    """The muxer with its MUXER_OPTIONS, for splitmuxsink's ``muxer`` ("matroskamux streamable=true")."""
    opts = MUXER_OPTIONS.get(muxer)
    return f"{muxer} {opts}" if opts else muxer


def label(fmt: str | None) -> str:
    return LABELS.get(str(fmt), str(fmt))


def _names(formats) -> str:
    """"AV1" / "H.265 and AV1" ("" for none)."""
    names = [LABELS[f] for f in FORMATS if f in set(formats)]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def crash_message(fmt: str, got: str | None) -> str:
    """"AV1 stopped working on this PC, so Momento switched to H.264" (the notification, status)."""
    text = f"{label(fmt)} stopped working on this PC"
    return f"{text}, so Momento switched to {label(got)}" if got and got != fmt else text


def crashed_note(formats) -> str:
    """The clip bar's note for crashed formats: "AV1 stopped working here. Pick it again to retry"."""
    what = _names(formats)
    if not what:
        return ""
    return f"{what} stopped working here. Pick {'it' if ' and ' not in what else 'one'} again to retry"


def unavailable_message(formats) -> str:
    """"Your graphics chip can't record AV1" / "... H.265 or AV1" ("" for none)."""
    names = [LABELS[f] for f in FORMATS if f in set(formats)]
    if not names:
        return ""
    what = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return f"Your graphics chip can't record {what}"


@dataclass
class Detection:
    """What the test encodes found. ``known`` is False when detection couldn't run
    (every present encoder is then assumed to work, and runtime fallback decides)."""

    vendor: str | None = None                          # "amd" | "intel" | "nvidia" | None
    works: dict[str, bool] = field(default_factory=dict)  # hardware encoder -> test encode passed
    present: tuple[str, ...] = ()                      # hardware encoders GStreamer has
    known: bool = True
    key: dict | None = None
    # format -> starts in it that crashed the process (START_GUARD), for this key
    crashes: dict[str, int] = field(default_factory=dict)

    def encoders(self, fmt: str) -> list[str]:
        """The hardware encoders of ``fmt`` to use, in order (only those that passed, when known)."""
        names = [n for n in HW_ENCODERS.get(fmt, ()) if n in self.present or not self.present]
        if not self.known:
            return names
        return [n for n in names if self.works.get(n)]

    def hardware(self, fmt: str) -> bool:
        return bool(self.encoders(fmt))

    def available(self, fmt: str) -> bool:
        """Can this machine record ``fmt``? H.264 always (software is its last resort).

        A format that crashed is still available (the user may pick it again to
        retry); ``plan`` skips it."""
        return fmt == "h264" or not self.known or self.hardware(fmt)

    def is_crashed(self, fmt: str) -> bool:
        """Did starts in ``fmt`` kill the process here (START_GUARD) often enough to skip it?"""
        return fmt in GUARDED and self.crashes.get(fmt, 0) >= CRASH_LIMIT

    def crashed(self) -> list[str]:
        return [f for f in FORMATS if self.is_crashed(f)]

    def auto(self, skip_crashed: bool = True) -> str:
        """The format Auto records in here (see AUTO_RULES); a format that crashed is skipped."""
        if not self.known:
            return "h264"

        def ok(fmt):
            return not (skip_crashed and self.is_crashed(fmt))
        for vendor, encoder, fmt in AUTO_RULES:
            if self.vendor == vendor and self.works.get(encoder) and ok(fmt):
                return fmt
        if self.hardware("h264"):
            return "h264"
        for fmt in FALLBACK:
            if self.hardware(fmt) and ok(fmt):
                return fmt
        return "h264"

    def to_json(self) -> dict:
        return {"version": PROBE_VERSION, "key": self.key, "vendor": self.vendor,
                "present": list(self.present), "works": dict(self.works),
                "crashes": {f: n for f, n in self.crashes.items() if n}, "checked": round(time.time())}

    @classmethod
    def from_json(cls, data: dict) -> Detection:
        works = data.get("works") or {}
        crashes = data.get("crashes") or {}
        return cls(vendor=data.get("vendor"), works={str(k): bool(v) for k, v in works.items()},
                   present=tuple(str(n) for n in data.get("present") or ()), known=True, key=data.get("key"),
                   crashes={str(k): int(v) for k, v in crashes.items() if str(k) in GUARDED})


UNKNOWN = Detection(known=False)


def plan(setting: str, det: Detection | None, failed=()) -> list[str]:
    """The formats to try, in order: the one to record in, then its fallbacks.

    ``setting`` is the configured format ("auto" -> what Auto picks here);
    ``failed`` the formats that failed to start in this process (skipped), and
    formats that crashed the process here (``Detection.is_crashed``) are skipped too.
    Formats this machine can't record are left out; H.264 is always last.
    """
    det = det or UNKNOWN
    try:
        setting = normalize(setting)
    except ValueError:
        setting = DEFAULT
    first = det.auto() if setting == "auto" else setting
    if first not in FALLBACK:
        first = "h264"
    order = FALLBACK[FALLBACK.index(first):]
    out = [f for f in order if f == "h264"
           or (det.available(f) and f not in failed and not det.is_crashed(f))]
    return out


def effective(setting: str, det: Detection | None, failed=()) -> str:
    """The format a recording with ``setting`` starts in here."""
    return plan(setting, det, failed)[0]


def crash_blocked(setting: str, det: Detection | None) -> str | None:
    """The format ``setting`` would record in here but that crashed the process (so it
    is skipped and another records); None when that isn't so."""
    if det is None:
        return None
    try:
        setting = normalize(setting)
    except ValueError:
        setting = DEFAULT
    first = det.auto(skip_crashed=False) if setting == "auto" else setting
    return first if det.is_crashed(first) else None


def allowed(det: Detection | None) -> list[str]:
    """CHOICES this machine can record ("auto" always)."""
    det = det or UNKNOWN
    return [c for c in CHOICES if c == "auto" or det.available(c)]


# --- the detection key ------------------------------------------------------------------

def gpus(root: str | Path = "/sys/class/drm") -> list[dict]:
    """Render nodes with their PCI vendor/device and kernel driver, in node order."""
    out = []
    try:
        nodes = sorted(Path(root).glob("renderD*"))
    except OSError:
        return out
    for node in nodes:
        dev = node / "device"
        info = {"node": node.name}
        for name in ("vendor", "device"):
            try:
                info[name] = (dev / name).read_text().strip().lower()
            except OSError:
                info[name] = ""
        try:
            info["driver"] = os.path.basename(os.readlink(dev / "driver"))
        except OSError:
            info["driver"] = ""
        out.append(info)
    return out


def vendor_of(gpu_list: list[dict]) -> str | None:
    """The vendor of the first render node (the one vah264enc & co. use)."""
    for gpu in gpu_list:
        return PCI_VENDORS.get(gpu.get("vendor", ""))
    return None


def va_vendor_string(node: str = "/dev/dri/renderD128") -> str:
    """libva's vendor string for a render node (the Mesa or Intel driver version); "" if unknown.

    vaInitialize + vaQueryVendorString through ctypes: ~25 ms, no encode.
    """
    import ctypes

    try:
        va = ctypes.CDLL("libva.so.2")
        vadrm = ctypes.CDLL("libva-drm.so.2")
    except OSError:
        return ""
    vadrm.vaGetDisplayDRM.restype = ctypes.c_void_p
    vadrm.vaGetDisplayDRM.argtypes = [ctypes.c_int]
    va.vaInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    va.vaQueryVendorString.restype = ctypes.c_char_p
    va.vaQueryVendorString.argtypes = [ctypes.c_void_p]
    va.vaTerminate.argtypes = [ctypes.c_void_p]
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError:
        return ""
    try:
        display = vadrm.vaGetDisplayDRM(fd)
        if not display:
            return ""
        major, minor = ctypes.c_int(), ctypes.c_int()
        if va.vaInitialize(display, ctypes.byref(major), ctypes.byref(minor)) != 0:
            return ""
        try:
            text = va.vaQueryVendorString(display)
            return text.decode("utf-8", "replace") if text else ""
        finally:
            va.vaTerminate(display)
    finally:
        os.close(fd)


def driver_version(gpu_list: list[dict]) -> str:
    """The driver's version for the key: libva's vendor string, and NVIDIA's module version."""
    parts = []
    if gpu_list and gpu_list[0].get("vendor") != "0x10de":
        parts.append(va_vendor_string(f"/dev/dri/{gpu_list[0]['node']}"))
    try:
        parts.append("nvidia " + Path("/sys/module/nvidia/version").read_text().strip())
    except OSError:
        pass
    return " | ".join(p for p in parts if p)


def present_encoders() -> tuple[str, ...]:
    """The hardware encoders of HW_ENCODERS that GStreamer has (imports GStreamer)."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    names = [n for fmt in FORMATS for n in HW_ENCODERS[fmt]]
    return tuple(n for n in names if Gst.ElementFactory.find(n) is not None)


def gst_version() -> str:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    return Gst.version_string()


def cache_key(present: tuple[str, ...], gpu_list: list[dict], driver: str, gst: str) -> dict:
    return {"probe": PROBE_VERSION, "encoders": sorted(present), "gpus": gpu_list, "driver": driver, "gst": gst}


def cache_path() -> Path:
    return config.CACHE_DIR / CACHE_NAME


def load_cache(path: Path, key: dict | None = None) -> Detection | None:
    """The cached detection; None when missing, unreadable, or (with ``key``) for another key."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != PROBE_VERSION:
        return None
    if key is not None and data.get("key") != key:
        return None
    try:
        return Detection.from_json(data)
    except (TypeError, ValueError, AttributeError):
        return None


def save_cache(path: Path, det: Detection) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(det.to_json(), indent=1) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("cannot write %s: %s", path, e)


def cached() -> Detection | None:
    """The last detection on disk, without checking its key (no GStreamer; for the CLI and the bar)."""
    return load_cache(cache_path())


# --- the test encode --------------------------------------------------------------------

def probe_pipeline(encoder: str) -> str:
    w, h = PROBE_SIZE
    return (f"videotestsrc num-buffers={PROBE_FRAMES} ! video/x-raw,format=NV12,width={w},height={h},"
            f"framerate=30/1 ! {encoder} name=enc ! fakesink name=sink sync=false")


def _probe_here(encoder: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """In this process: encode PROBE_FRAMES frames with ``encoder``; True when it reaches EOS
    with at least one encoded buffer and no error."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    try:
        pipeline = Gst.parse_launch(probe_pipeline(encoder))
    except Exception:  # noqa: BLE001 - GLib.Error: no such element, bad caps
        return False
    count = [0]

    def on_buffer(_pad, _info):
        count[0] += 1
        return Gst.PadProbeReturn.OK

    pipeline.get_by_name("sink").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    ok = False
    try:
        if pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE:
            msg = pipeline.get_bus().timed_pop_filtered(int(timeout * Gst.SECOND),
                                                        Gst.MessageType.EOS | Gst.MessageType.ERROR)
            ok = msg is not None and msg.type == Gst.MessageType.EOS and count[0] > 0
    finally:
        pipeline.set_state(Gst.State.NULL)
    return ok


def probe_encoders(names, timeout: float = PROBE_TIMEOUT) -> dict[str, bool]:
    """Test-encode with each encoder in a child process; encoder -> it works.

    One child for all of them (``python -m momento.codecs probe ...``), printing one
    line per encoder as it finishes, so an encoder that crashes or hangs the child
    only costs itself (and those after it, which are retried in a new child).
    """
    names = list(names)
    results: dict[str, bool] = {}
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("LIBVA_MESSAGING_LEVEL", "1")   # errors only: no "libva info" lines per encoder
    while names:
        cmd = [sys.executable, "-m", "momento.codecs", "probe", f"--timeout={timeout:g}", *names]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                  timeout=timeout * len(names) + 10)
            out = proc.stdout
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        except OSError as e:
            log.warning("cannot run the encoder test: %s", e)
            break
        for line in out.splitlines():
            try:
                d = json.loads(line)
                results[str(d["encoder"])] = bool(d["ok"])
            except (ValueError, KeyError, TypeError):
                continue
        rest = [n for n in names if n not in results]
        if rest:
            results[rest[0]] = False      # the one it died (or hung) on
            log.warning("encoder test: %s crashed or hung", rest[0])
        names = rest[1:]
    return results


def detect(present: tuple[str, ...], gpu_list: list[dict], key: dict, probe=probe_encoders) -> Detection:
    started = time.monotonic()
    works = probe(present) if present else {}
    det = Detection(vendor=vendor_of(gpu_list), works={n: bool(works.get(n)) for n in present},
                    present=tuple(present), known=True, key=key)
    log.info("video formats: tested %s in %.1fs: %s; Auto records %s", ", ".join(present) or "no encoders",
             time.monotonic() - started,
             ", ".join(f"{n} {'ok' if ok else 'failed'}" for n, ok in det.works.items()) or "nothing",
             LABELS[det.auto()])
    return det


def _sandboxed() -> bool:
    return bool(os.environ.get("MOMENTO_TEST_SANDBOX"))


class Detector:
    """Runs detection once per process, off the caller's thread, and caches it on disk.

    ``ready()`` is non-blocking; ``ensure(callback)`` starts detection if needed and
    calls ``callback(detection)`` from the worker thread (or at once, when ready);
    ``wait(timeout)`` blocks (worker threads only). ``failed`` holds the formats
    that failed to start in this process, so later starts skip them.

    Under the test sandbox a Detector made without ``probe``/``key`` never runs a
    test encode: it reports an empty (known) detection, so Auto is H.264.

    ``record_crash`` counts a format start that killed the last process (see
    START_GUARD); it is applied before the detection is handed to anyone, so the
    first start already skips the format. ``clear_crash`` forgets it (a retry).
    """

    def __init__(self, probe=None, key=None, path: Path | None = None):
        self._probe = probe
        self._key = key
        self._path = path
        self._fake = probe is None and key is None and _sandboxed()
        self._fake_det: Detection | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._result: Detection | None = None
        self._running = False
        self._callbacks: list = []
        self._crash_reports: list[tuple[str, dict | None]] = []   # waiting for the detection
        self.failed: set[str] = set()
        # Formats that crashed the last process while the detection was unknown: in
        # ``failed`` too (skipped for this process), kept apart for the words.
        self.crashed_unknown: set[str] = set()

    def ready(self) -> Detection | None:
        if self._fake:
            if self._fake_det is None:
                self._fake_det = Detection(known=True)
            return self._fake_det
        return self._result

    def ensure(self, callback=None) -> None:
        det = self.ready()
        if det is not None:
            if callback is not None:
                callback(det)
            return
        with self._lock:
            if self._result is not None:
                det = self._result
            else:
                if callback is not None:
                    self._callbacks.append(callback)
                if self._running:
                    return
                self._running = True
        if det is not None:
            if callback is not None:
                callback(det)
            return
        threading.Thread(target=self._run, name="formats", daemon=True).start()

    def wait(self, timeout: float | None = None) -> Detection | None:
        det = self.ready()
        if det is not None:
            return det
        self.ensure()
        self._done.wait(timeout)
        return self._result

    def forget(self) -> None:
        """Detect again on the next ensure() (tests; a GPU hot-plug)."""
        with self._lock:
            if not self._running:
                self._result = None
                self._fake_det = None
                self._done.clear()
                self.failed.clear()
                self.crashed_unknown.clear()

    # --- crashes (START_GUARD) ---

    def record_crash(self, fmt: str, key: dict | None) -> None:
        """The last process died starting ``fmt`` with the detection ``key``: count it.

        Applied now when the detection is ready, else as soon as it is (before
        anyone gets it). Counted in formats.json only when ``key`` is this
        detection's (the same GPU and driver); a crash with an unknown detection
        skips the format for this process only.
        """
        if fmt not in GUARDED:
            return
        with self._lock:
            det = self.ready()
            if det is None:
                self._crash_reports.append((fmt, key))
                return
            self._count_crash(det, fmt, key)

    def _count_crash(self, det: Detection, fmt: str, key: dict | None) -> None:
        """(holds _lock) One crash of ``fmt`` into ``det``, saved with it."""
        if not det.known or key is None and not self._fake:
            self.failed.add(fmt)
            self.crashed_unknown.add(fmt)
            log.warning("%s crashed Momento while starting; skipping it until Momento restarts "
                        "(what this PC records isn't known)", LABELS[fmt])
            return
        if det.key != key:
            log.info("%s crashed Momento while starting, but with another driver or GPU: not counted",
                     LABELS[fmt])
            return
        det.crashes[fmt] = det.crashes.get(fmt, 0) + 1
        log.warning("%s crashed Momento while starting (%d time%s on this driver)%s", LABELS[fmt],
                    det.crashes[fmt], "" if det.crashes[fmt] == 1 else "s",
                    "; skipping it until the driver changes or it is picked again" if det.is_crashed(fmt) else "")
        if not self._fake:
            save_cache(self._path or cache_path(), det)

    def clear_crash(self, fmt: str) -> bool:
        """Forget that ``fmt`` crashed (the user picked it again); True if it had."""
        with self._lock:
            had = fmt in self.crashed_unknown
            if had:
                self.failed.discard(fmt)
                self.crashed_unknown.discard(fmt)
            self._crash_reports = [r for r in self._crash_reports if r[0] != fmt]
            det = self.ready()
            if det is not None and det.crashes.get(fmt):
                had = True
                del det.crashes[fmt]
                if not self._fake and det.known:
                    save_cache(self._path or cache_path(), det)
        if had:
            log.info("%s picked again: trying it", LABELS.get(fmt, fmt))
        return had

    def _compute_key(self):
        if self._key is not None:
            return self._key()
        present = present_encoders()
        gpu_list = gpus()
        return present, gpu_list, cache_key(present, gpu_list, driver_version(gpu_list), gst_version())

    def _run(self) -> None:
        det = None
        try:
            present, gpu_list, key = self._compute_key()
            path = self._path or cache_path()
            det = load_cache(path, key)
            if det is not None:
                log.info("video formats (cached): %s; Auto records %s",
                         ", ".join(f"{n} {'ok' if ok else 'failed'}" for n, ok in det.works.items()) or "none",
                         LABELS[det.auto()])
            else:
                det = detect(present, gpu_list, key, self._probe or probe_encoders)
                save_cache(path, det)
        except Exception:  # noqa: BLE001 - never leave a start waiting
            log.exception("video format detection failed; every format is tried as it comes")
            det = Detection(known=False)
        with self._lock:
            reports, self._crash_reports = self._crash_reports, []
            for fmt, key in reports:
                self._count_crash(det, fmt, key)
            self._result = det
            self._running = False
            callbacks, self._callbacks = self._callbacks, []
            self._done.set()
        for cb in callbacks:
            try:
                cb(det)
            except Exception:  # noqa: BLE001
                log.exception("format detection callback failed")


DETECTOR = Detector()


class StartGuard:
    """The marker of a format start that hasn't proven stable yet (see GUARD_NAME).

    ``begin`` writes it (a GUARDED format; any other clears it), ``clear`` removes
    it (stable, or a clean stop), ``recover`` (daemon start) returns and removes
    the one a dead process left behind.
    """

    def __init__(self, path: Path | None = None):
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or config.CACHE_DIR / GUARD_NAME

    def read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def begin(self, fmt: str, encoder: str = "", key: dict | None = None) -> None:
        if fmt not in GUARDED:
            self.clear()
            return
        data = {"format": fmt, "encoder": encoder, "key": key, "pid": os.getpid(), "started": round(time.time(), 3)}
        path = self.path
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data) + "\n")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("cannot write %s: %s", path, e)

    def clear(self) -> None:
        """Remove this process's marker (another live process's is left alone)."""
        data = self.read()
        if data is None or data.get("pid") in (None, os.getpid()) or not _momento_alive(data.get("pid")):
            try:
                self.path.unlink()
            except OSError:
                pass

    def recover(self) -> dict | None:
        """At daemon start: the marker a process that died while starting a format left
        (removed now); None if there is none, or its process is still running."""
        data = self.read()
        if data is None:
            return None
        pid = data.get("pid")
        if pid != os.getpid() and _momento_alive(pid):
            return None
        try:
            self.path.unlink()
        except OSError:
            pass
        return data if data.get("format") in GUARDED else None


def _momento_alive(pid) -> bool:
    """Is ``pid`` a running Momento (python) process?"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"momento" in cmd


START_GUARD = StartGuard()


def _main(argv: list[str]) -> int:
    """``python -m momento.codecs probe [--timeout=S] ENCODER...``: one JSON line per encoder."""
    if not argv or argv[0] != "probe":
        print("usage: python -m momento.codecs probe [--timeout=S] ENCODER...", file=sys.stderr)
        return 2
    timeout = PROBE_TIMEOUT
    names = []
    for arg in argv[1:]:
        if arg.startswith("--timeout="):
            timeout = float(arg.split("=", 1)[1])
        else:
            names.append(arg)
    for name in names:
        ok = _probe_here(name, timeout)
        print(json.dumps({"encoder": name, "ok": ok}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
