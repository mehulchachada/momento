"""GStreamer capture pipeline: screen + audio -> H.264/H.265/AV1 + AAC -> segments.

The video format (``[capture] format``: auto, h264, h265, av1) picks the
encoder, parser and segment container (``codecs.CONTAINERS``: MPEG-TS for
H.264/H.265, Matroska for AV1). Auto asks ``codecs.DETECTOR`` what this machine
records well; a start waits for that detection (cached on disk, so only the
first start after a driver change runs the test encodes). If the format's
encoder fails before the first segment, the next format of ``codecs.FALLBACK``
that works is used (``format_fallback`` says so; the daemon notifies once).

Segments are written by splitmuxsink into the buffer directory; every
fragment open/close is reported to the RingBuffer with wall-clock times, the
capture session id and the negotiated stream parameters. The RingBuffer keeps
an on-disk index of them, so footage from earlier sessions (before a pause,
setting change or restart) stays saveable.

Window mode (``[capture] target = "window"``, portal source): the portal is
asked for one window instead of a monitor. When that window closes, the stream
ends; capture then stops in state ``no_window`` with no automatic retry (a
retry could open the picker again and again). The segment being written is
finished first; what happens to the buffer is the daemon's call (it treats a
closed window like the user pressing Stop). Any other failure in window mode
stops in ``error``, also without a retry. A window's size can change
mid-stream: it is scaled (with black bars) to the configured resolution, and
with ``native`` the output size is locked to the first size of the session, so
the encoder output never changes inside a session.

Never upscaled: the captured picture's size (``source_size``) caps the
resolution. It is known before the pipeline is built when the portal says how
big the stream is (the test source always knows), so the "size" capsfilter is
created at the final output size and nothing renegotiates once the stream runs:
changing those caps at runtime makes pipewiresrc renegotiate with the
compositor, which drops all its buffers (KWin), and that looks like the window
closing. Otherwise the first caps on the source pad tell, and the capsfilter is
changed then (a fallback; an early "buffers removed" error after it restarts
the pipeline once at the now-known size instead of ending capture).

A preset taller than the source (``quality.fits_source``) records at the
source's own size instead, as if ``native`` were set, with the bitrate of that
size (``resolution_effective`` says which was used). The config is left alone.
Never taller than ``quality.MAX_HEIGHT`` (1080) either: ``native`` scales a
taller picture down to fit (``quality.native_size``, aspect kept).

Frame rate: ``[capture] fps`` is 60, 120 or "auto" (the default), which follows
the refresh rate of the screen being recorded (``quality.auto_fps``: 120 from
100 Hz up, else 60). The refresh comes from the stream itself: PipeWire
screencasts announce it in their caps (KWin and Mutter as ``max-framerate``,
the output's refresh, with ``framerate`` 0/1; wlroots as ``framerate``). The
pipeline is built at the last known refresh (``refresh_hz``: from an earlier
session, or the daemon; unknown: 60 fps) and the first caps settle it. Only
videorate's output caps (the "rate" capsfilter) and the encoder's GOP and
bitrate change then, before any frame reaches them; the RECONFIGURE event that
change sends upstream is dropped at videorate, which converts any input rate,
so the compositor's stream is never renegotiated and nothing restarts. A
refresh change mid-session is only logged: the next start records at it.

Everything here runs on the GLib main loop of the caller.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from . import codecs, config, quality  # noqa: E402
from .ringbuffer import RingBuffer, segment_number  # noqa: E402

log = logging.getLogger("momento.pipeline")

Gst.init(None)

# H.264: every hardware encoder, then software as the last resort (never for H.265/AV1).
ENCODER_ORDER = list(codecs.HW_ENCODERS["h264"] + codecs.SW_ENCODERS["h264"])
# Encoders that take VAMemory NV12 straight from vapostproc (GPU colour conversion):
# the "va" plugin's (vah264enc, vah265enc, vaav1enc, their low-power twins).
VA_ENCODERS = {n for names in codecs.HW_ENCODERS.values() for n in names
               if n.startswith("va") and not n.startswith("vaapi")}
RETRY_SECONDS = 3
STOP_TIMEOUT = 3.0
# A fragment that closes up to this long before a flush request still counts as
# covering it: the forced keyframe is stamped with its capture time, which is a
# few frames of pipeline latency behind the moment flush() was called.
FLUSH_TOLERANCE = 0.25
# A PipeWire source that fails this soon after the pipeline started, or after the
# output caps were changed at runtime, is most likely renegotiating (the
# compositor drops every buffer), not a closed window: restart once first.
RENEGOTIATE_GRACE = 3.0

# Screenshots: a frame that reaches the encoder this long after the request was
# certainly captured after it (the queues hold ~0.15 s at most), whatever its
# timestamp says; guards against a source whose timestamps run behind.
FRAME_IN_FLIGHT_MAX = 0.5

# Window mode messages; the clip bar shows its own wording. WINDOW_CLOSED and
# WINDOW_NOT_PICKED come with state "no_window", WINDOW_STOPPED with "error".
WINDOW_CLOSED = "The game window closed \u2014 pick a window to keep recording"
WINDOW_NOT_PICKED = "No game window picked \u2014 press play to pick one"
WINDOW_STOPPED = "Window capture stopped \u2014 press play to try again"

# Debug A/B: with this set to 1 the video source feeds a fakesink directly (no
# scaling, encoding, muxing or audio; nothing is recorded), so the cost of the
# compositor's screencast alone can be told apart from Momento's encoding.
# An alias of MOMENTO_DEBUG_STAGE=capture.
CAPTURE_ONLY_ENV = "MOMENTO_DEBUG_CAPTURE_ONLY"

# Debug A/B: build only part of the chain, to find which stage costs frames.
#   capture   the source into a fakesink (as MOMENTO_DEBUG_CAPTURE_ONLY=1)
#   convert   source -> the real video chain up to the encoder (vapostproc, its
#             NV12/size caps, queue, videorate) -> fakesink; nothing is written
#   encode    ... -> the encoder, same settings -> fakesink; nothing is written
#   noaudio   the normal recording without the audio branch
#   lowpower  the normal recording with the encoder at its cheapest (LOWPOWER_VA)
#   h265      the normal recording in H.265: the same as [capture] format = "h265"
#   av1       the normal recording in AV1 (Matroska segments): format = "av1"
#   lowbitrate  the normal recording at LOWBITRATE_SHARE of the bitrate
#   vbr       the normal recording with QVBR (or VBR) rate control, same target bitrate
#   lowprio   the normal recording with the encoder made to yield (LOWPRIO_QUEUE)
# Anything else records normally (with a warning).
STAGE_ENV = "MOMENTO_DEBUG_STAGE"
STAGES = ("capture", "convert", "encode", "noaudio", "lowpower", "h265", "av1", "lowbitrate", "vbr", "lowprio")

# lowpower: vah264enc/vah264lpenc settings. On radeonsi (Mesa's va frontend)
# target-usage is not a 1 (best) .. 7 (fastest) scale: any value but 1 is read as
# bits (bit 0 unused, bits 1-2 VCN preset 0 speed / 1 balance / 2 quality /
# 3 high quality, bit 3 pre-encode, bit 4 VBAQ). GStreamer's default 4 is the
# *quality* preset and 7 is *high quality*, the dearest. 2 is the balance preset
# with no pre-encode and no VBAQ: the cheapest reachable in GStreamer's 1-7 range
# (speed would need 0, 8, 16 or 24). On Intel (iHD) 7 would be the fastest.
# One reference frame and no 8x8 transform trim motion search / transform work;
# rate control stays CBR at the same bitrate, so the written bytes compare.
LOWPOWER_VA = {"target_usage": 2, "ref_frames": 1, "dct8x8": False}

# h265 / av1 debug stages: they force that format (as [capture] format would).
CODEC_STAGES = {"h265": "h265", "av1": "av1"}
# The parser + caps after the encoder, per format. Every format gets the same
# bitrate table, a 1 s GOP and no B-frames (AV1 has none: no future references
# instead, see _encoder_settings).
H264_PARSE = "h264parse name=parse config-interval=-1 ! video/x-h264,stream-format=byte-stream"
PARSERS = {
    "h264": H264_PARSE,
    "h265": "h265parse name=parse config-interval=-1 ! video/x-h265,stream-format=byte-stream",
    "av1": "av1parse name=parse ! video/x-av1,stream-format=obu-stream,alignment=tu",
}

# lowbitrate: the share of the normal bitrate (10 Mbps Standard 1080p60 -> 6 Mbps).
LOWBITRATE_SHARE = 0.6

# vbr: the first rate control the driver offers (radeonsi 26.2 offers QVBR). The
# "bitrate" stays the target; GStreamer's va encoders make the maximum
# bitrate * 100 / target-percentage (66 by default).
VBR_MODES = ("qvbr", "vbr")

# lowprio: make the encoder yield to the game. A lower-priority VA context was
# looked for first and can't be had: Mesa 26.2.1's VA frontend has no
# VAConfigAttribContextPriority and doesn't handle VAContextParameterUpdateBuffer,
# and it creates its context with pipe_create_multimedia_context(), which never
# passes PIPE_CONTEXT_LOW_PRIORITY (radeonsi's amdgpu winsys would turn that into
# AMDGPU_CTX_PRIORITY_LOW; no env var sets it); GStreamer's va encoders have no
# priority property. So the CPU side yields instead: a 2-frame leaky queue right
# before the encoder, so frames are dropped when it falls behind instead of
# piling up, and that queue's streaming thread (it runs the encoder and parser)
# at SCHED_IDLE and nice LOWPRIO_NICE. (radeonsi may still submit the encode job
# from its own winsys thread, which keeps its priority.)
LOWPRIO_QUEUE = "queue name=encq max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream"
LOWPRIO_NICE = 19

FAKESINK = "fakesink name=sink sync=false async=false enable-last-sample=false"


def capture_only() -> bool:
    return os.environ.get(CAPTURE_ONLY_ENV, "").strip().lower() not in ("", "0", "false", "no", "off")


def debug_stage() -> str | None:
    """The MOMENTO_DEBUG_STAGE debug variant to build, or None for a normal recording."""
    value = os.environ.get(STAGE_ENV, "").strip().lower()
    if value in STAGES:
        return value
    if value:
        log.warning("%s=%r is not one of %s: ignored (a normal recording)",
                    STAGE_ENV, value, ", ".join(STAGES))
    return "capture" if capture_only() else None


def native_display_sizes(root: str | Path = "/sys/class/drm") -> set[tuple[int, int]]:
    """The native (preferred) mode of every connected display, both orientations.

    Read from /sys/class/drm (the first line of a connector's ``modes``); empty
    when nothing can be read.
    """
    sizes: set[tuple[int, int]] = set()
    try:
        connectors = sorted(Path(root).glob("card*-*"))
    except OSError:
        return sizes
    for conn in connectors:
        try:
            if (conn / "status").read_text().strip() != "connected":
                continue
            first = (conn / "modes").read_text().split("\n", 1)[0].strip()
        except OSError:
            continue
        m = re.fullmatch(r"(\d+)x(\d+)\S*", first)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            sizes.update({(w, h), (h, w)})
    return sizes


@dataclass(frozen=True)
class Frame:
    """One video frame as the encoder receives it (after scaling), for a screenshot.

    ``buffer`` may live in GPU memory (VAMemory); ``va_context`` is the recording
    pipeline's VA display, so a converter can read the surface (see screenshot.py).
    """

    buffer: Gst.Buffer
    caps: Gst.Caps
    va_context: Gst.Context | None = None

    @property
    def size(self) -> tuple[int, int]:
        st = self.caps.get_structure(0)
        return st.get_int("width")[1], st.get_int("height")[1]


# The context type the va plugin shares its display under.
VA_DISPLAY_CONTEXT = "gst.va.display.handle"


@dataclass(frozen=True)
class _Variant:
    encoder: str
    zero_copy: bool

    def __str__(self) -> str:
        return f"{self.encoder} ({'zero-copy vapostproc' if self.zero_copy else 'videoconvert'})"


def describe_capture(size: tuple[int, int] | None, fps: int, quality_name: str, kbps: int, fmt: str | None,
                     encoder: str, zero_copy: bool, window: bool, source: str, *, kbps_by_hand: bool = False,
                     fallback: tuple[str, str] | None = None, stage: str | None = None,
                     fps_why: str | None = None) -> str:
    """Everything that defines a recording, as the log's one line says it::

        1280x720 @ 120 fps (auto: 120 Hz screen), ultra, 22000 kbps, AV1 (vaav1enc, zero-copy), full screen, portal

    ``size`` is the picture really recorded (None: not known before the first frame).
    ``fps_why``: why this frame rate, for fps "auto" ("auto: 120 Hz screen").
    ``window``: one window is recorded (never its title), else the full screen.
    ``fallback``: (wanted, got) when the wanted format didn't start.
    """
    shown = f"{size[0]}x{size[1]}" if size else "size from the first frame"
    how = [encoder, "zero-copy" if zero_copy else "not zero-copy"]
    fmt_part = f"{codecs.label(fmt)} ({', '.join(how)}"
    if fallback:
        fmt_part += f"; {codecs.label(fallback[0])} didn't start"
    fmt_part += ")"
    parts = [f"{shown} @ {fps} fps" + (f" ({fps_why})" if fps_why else ""), str(quality_name), f"{kbps} kbps" + (" (set by hand)" if kbps_by_hand else ""),
             fmt_part, "window" if window else "full screen", source or "?"]
    if stage:
        parts.append(f"debug stage {stage}")
    return ", ".join(parts)


def _have(factory: str) -> bool:
    return Gst.ElementFactory.find(factory) is not None


def _set(element: Gst.Element, **props) -> None:
    """Set properties that exist on this element version; ignore the rest."""
    for name, value in props.items():
        name = name.replace("_", "-")
        if element.find_property(name) is None:
            log.debug("%s has no property %s", element.get_factory().get_name(), name)
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        Gst.util_set_object_arg(element, name, str(value))


def gamescope_node_exists() -> bool:
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3)
        objs = json.loads(out.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    for obj in objs:
        props = ((obj.get("info") or {}).get("props")) or {}
        if obj.get("type", "").endswith(":Node") and props.get("node.name") == "gamescope":
            return True
    return False


def resolve_source(requested: str) -> str:
    if requested != "auto":
        return requested
    env = os.environ
    if "gamescope" in env.get("XDG_CURRENT_DESKTOP", "").lower() or env.get("GAMESCOPE_WAYLAND_DISPLAY"):
        if gamescope_node_exists():
            return "gamescope"
    if env.get("WAYLAND_DISPLAY"):
        return "portal"
    if env.get("DISPLAY"):
        return "x11"
    return "portal"


class Recorder:
    # The "test" source's picture size (a stand-in for a 1080p screen); tests set
    # another one on an instance to stand in for a smaller screen or a window.
    test_size = (1920, 1080)
    # The test source says its size in advance, as a portal does; tests turn this
    # off to exercise the runtime fallback (the size learnt from the first caps).
    test_size_known = True
    # The test source's screen refresh (Hz), announced in its caps as a KWin
    # screencast does (max-framerate); None: its caps say only its own frame rate.
    test_refresh: float | None = None

    def __init__(
        self,
        cfg: dict,
        ring: RingBuffer,
        on_state: Callable[[str, str | None], None],
        bus=None,
    ):
        self.cfg = cfg
        self.ring = ring
        self.on_state = on_state
        self._dbus = bus
        self.recording = False
        self.state = "stopped"
        self.source_name = ""
        self.encoder_name = ""
        self.buffer_dir = Path(cfg["buffer"]["dir"])
        # The frame rate: the setting ("auto" | 60 | 120) and the one recorded. For
        # auto it follows refresh_hz, the recorded screen's refresh rate: the last one
        # known (the daemon may set it before start()), settled by the first caps.
        self.fps_setting = quality.fps_setting(cfg["capture"])
        self.refresh_hz: float | None = None
        self.fps = quality.fps(cfg["capture"])
        # The preset recorded (an older config's 1440p/2160p records at 1080p).
        self.size_name = quality.configured(cfg["capture"])
        self.size = quality.RESOLUTIONS[self.size_name]
        # The captured picture's size, from the first caps of the current (or last)
        # session, and the resolution actually recorded (the configured one, or
        # "native" when that is taller than the source). None until known.
        self.source_size: tuple[int, int] | None = None
        self.resolution_effective: str | None = None
        self.target = config.capture_target(cfg["capture"])
        self.window_mode = False  # target "window" on a source that can do it (set by start())
        # The video format: the setting ("auto" | "h264" | "h265" | "av1"), the one
        # the current (or last) pipeline records in, and (wanted, got) when the
        # wanted one failed to start and a fallback records instead.
        self.format = codecs.configured(cfg["capture"])
        self.format_effective: str | None = None
        self.format_fallback: tuple[str, str] | None = None
        self._formats: list[str] = []            # the plan: the format to record in, then fallbacks
        self._detection: codecs.Detection | None = None
        self._awaiting_formats = False           # start() waits for codecs.DETECTOR
        self._start_gen = 0
        self._stable_id = 0                      # timer: the format has recorded STABLE_SECONDS

        self._pipeline: Gst.Pipeline | None = None
        self._bus_watch = None
        self._portal = None
        self._pw_fd: int | None = None
        self._pw_node: int | None = None
        self._variants: list[_Variant] = []
        self._variant_idx = 0
        self._stop_requested = True
        self._retry_id = 0
        self._start_wall: float | None = None
        self._got_fragment = False
        self._next_index = 0
        self._session: str | None = None
        self._params: dict | None = None
        self._open: dict[str, float] = {}
        self._flush_waiters: list[list] = []  # [request_wall, callback, timeout_id]
        self._size_caps: str | None = None      # caps of the "size" capsfilter, without width/height
        self._rate_mem = "video/x-raw"          # the "rate" capsfilter's media type (VAMemory: zero-copy)
        self._locked_size: tuple[int, int] | None = None
        # The source's size before the pipeline is built (the portal's stream size,
        # the test source's size, or the real size after a restart); None: unknown.
        self._known_size: tuple[int, int] | None = None
        self._source_seen = False
        self._settle_from = 0.0          # monotonic time of the start / last runtime caps change
        self._restarted = False          # a renegotiation restart was used (once per start)
        self._kbps = 0  # the encoder's bitrate as last set
        self._stage: str | None = None  # the MOMENTO_DEBUG_STAGE of the pipeline last built
        self._frame_waiters: list[dict] = []    # grab_frame() requests still waiting for a frame
        self._variant: _Variant | None = None   # the variant of the pipeline last built
        self._logged: tuple | None = None       # _log_key() of the last "recording:" line

    # --- public API -------------------------------------------------------------

    def start(self, interactive: bool = False) -> None:
        """Start capturing.

        ``interactive`` marks a start the user asked for (resume, pick a window,
        switching to window mode). In window mode only such a start may open the
        window picker: an automatic one (daemon start, a reload, free space
        coming back) without a stored window token stops in "no_window" instead.
        """
        if self._pipeline is not None or (self._portal is not None and not self._stop_requested):
            return
        if self._awaiting_formats:
            return  # already starting: waiting for the format detection
        self._stop_requested = False
        self._cancel_retry()
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        # Keep earlier footage: reconcile the on-disk index, number after it.
        self.ring.attach(self.buffer_dir)
        self._next_index = max(self._next_index, self.ring.recover())
        self.source_name = resolve_source(self.cfg["capture"]["source"])
        # "test" stands in for a window too, so window mode can be exercised without a portal.
        self.window_mode = self.target == "window" and self.source_name in ("portal", "test")
        if self.target == "window" and not self.window_mode:
            log.warning("window capture needs the screen-share portal; recording the whole %s source",
                        self.source_name)
        if (self.window_mode and self.source_name == "portal" and not interactive
                and not config.portal_token_path("window").exists()):
            # Nothing to restore and nobody asked: don't pop the picker on our own.
            self._stop_requested = True
            self._set_state("no_window", WINDOW_NOT_PICKED)
            return
        det = self._formats_known()
        if det is None:
            # What this machine records isn't known yet (the first start after a
            # driver change): the test encodes run in a thread, then capture starts.
            self._awaiting_formats = True
            self._start_gen += 1
            gen = self._start_gen
            self._set_state("starting")
            codecs.DETECTOR.ensure(lambda _det: GLib.idle_add(self._formats_detected, gen))
            return
        self._start_capture(det)

    def _formats_known(self) -> codecs.Detection | None:
        """The detection to plan with; None while it is still running.

        Not needed (UNKNOWN is enough) for a named encoder or plain H.264, whose
        encoders are all tried in order anyway.
        """
        encoder = self.cfg["capture"].get("encoder", "auto")
        if encoder not in ("", "auto") or self._wanted_format() == "h264":
            return codecs.DETECTOR.ready() or codecs.UNKNOWN
        return codecs.DETECTOR.ready()

    def _formats_detected(self, gen: int) -> bool:
        if gen != self._start_gen or not self._awaiting_formats:
            return False  # stopped (and maybe started again) meanwhile
        self._awaiting_formats = False
        if self._stop_requested or self._pipeline is not None:
            return False
        self._start_capture(codecs.DETECTOR.ready() or codecs.UNKNOWN)
        return False

    def _wanted_format(self) -> str:
        """The format setting, or the h265/av1 debug stage's."""
        stage = debug_stage()
        return CODEC_STAGES.get(stage, self.format)

    def _start_capture(self, det: codecs.Detection) -> None:
        self._detection = det
        wanted = self._wanted_format()
        if wanted != self.format:
            log.warning("%s=%s: debug A/B. Recording in %s, as [capture] format = \"%s\" would",
                        STAGE_ENV, wanted, codecs.label(wanted), wanted)
        self._formats = codecs.plan(wanted, det, codecs.DETECTOR.failed)
        self.format_fallback = None
        self._variants = self._plan_variants()
        self._variant_idx = 0
        if not self._variants:
            self._fatal("no usable video encoder found (tried: %s)" % ", ".join(
                n for f in self._formats for n in self._format_encoders(f)))
            return
        self._set_state("starting")
        self._begin()

    def stop(self) -> None:
        # A clean stop: whatever format was starting didn't crash us (START_GUARD).
        # First, before the drain below, which a stuck driver could make slow.
        codecs.START_GUARD.clear()
        self._cancel_stable()
        self._stop_requested = True
        self._awaiting_formats = False
        self._start_gen += 1
        self._cancel_retry()
        self._teardown(graceful=True)
        self._close_portal()
        self._fire_flush_waiters()
        if self.state != "stopped":
            self._set_state("stopped")

    def flush(self, callback: Callable[[], None], timeout: float = 5.0) -> None:
        """Close the current segment now; call back once footage up to now is on disk."""
        if self._pipeline is None or not self.recording:
            GLib.idle_add(_once(callback))
            return
        waiter = [time.time(), callback, 0]
        waiter[2] = GLib.timeout_add(int(timeout * 1000), self._flush_timeout, waiter)
        self._flush_waiters.append(waiter)
        enc = self._pipeline.get_by_name("enc")
        if enc is not None:
            enc.send_event(GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))
        mux = self._pipeline.get_by_name("mux")
        if mux is None:  # a debug stage without a mux (capture, convert, encode): nothing is written
            self._fire_flush_waiters()
            return
        mux.emit("split-now")

    def grab_frame(self, callback: Callable[[Frame | None], None], timeout: float = 3.0) -> None:
        """Hand the next captured frame to ``callback(frame)`` on the main loop (screenshots).

        The frame is the one the encoder gets: after scaling, so it matches the
        recording (the picked window in window mode, the screen otherwise). Only
        frames captured after this call count, so nothing that was already on its
        way through the pipeline is taken. Nothing runs until it is called: a
        one-shot buffer probe sits on the "size" capsfilter until the frame
        arrives. ``frame`` is None when capture isn't running, stops first, or no
        frame comes within ``timeout`` seconds.
        """
        pipeline = self._pipeline
        size = pipeline.get_by_name("size") if pipeline is not None and self.recording else None
        if size is None:
            GLib.idle_add(_once(lambda: callback(None)))
            return
        if self._start_wall is None:
            self._compute_start_wall()
        waiter = {"callback": callback, "after": time.time(), "start_wall": self._start_wall,
                  "context": pipeline.get_context(VA_DISPLAY_CONTEXT), "pad": size.get_static_pad("src"),
                  "fired": False, "probe": 0, "timer": 0}
        waiter["probe"] = waiter["pad"].add_probe(Gst.PadProbeType.BUFFER, self._frame_probe, waiter)
        waiter["timer"] = GLib.timeout_add(int(timeout * 1000), self._frame_timeout, waiter)
        self._frame_waiters.append(waiter)

    def _frame_probe(self, pad: Gst.Pad, info: Gst.PadProbeInfo, waiter: dict):
        """Streaming thread: keep a reference to the first frame captured after the request."""
        if waiter["fired"]:
            return Gst.PadProbeReturn.REMOVE
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        if (buf.pts != Gst.CLOCK_TIME_NONE and waiter["start_wall"] is not None
                and waiter["start_wall"] + buf.pts / Gst.SECOND < waiter["after"]
                and time.time() - waiter["after"] < FRAME_IN_FLIGHT_MAX):
            return Gst.PadProbeReturn.OK  # captured before the request: still in flight
        caps = pad.get_current_caps()
        if caps is None:
            return Gst.PadProbeReturn.OK
        waiter["fired"] = True
        # The buffer stays ours (a reference, no copy) until the screenshot is written.
        GLib.idle_add(self._frame_ready, waiter, Frame(buf, caps, waiter["context"]))
        return Gst.PadProbeReturn.REMOVE

    def _frame_ready(self, waiter: dict, frame: Frame | None) -> bool:
        if waiter in self._frame_waiters:
            self._frame_waiters.remove(waiter)
            if waiter["timer"]:
                GLib.source_remove(waiter["timer"])
                waiter["timer"] = 0
            _once(lambda: waiter["callback"](frame))()
        return False

    def _frame_timeout(self, waiter: dict) -> bool:
        waiter["timer"] = 0
        if waiter in self._frame_waiters:
            log.warning("no video frame for a screenshot within the timeout")
            waiter["fired"] = True
            try:
                waiter["pad"].remove_probe(waiter["probe"])
            except Exception:  # noqa: BLE001 - already removed with its pipeline
                pass
            self._frame_ready(waiter, None)
        return False

    def _fail_frame_waiters(self) -> None:
        for waiter in list(self._frame_waiters):
            waiter["fired"] = True
            GLib.idle_add(self._frame_ready, waiter, None)

    # --- startup ------------------------------------------------------------------

    def _format_encoders(self, fmt: str) -> list[str]:
        """The encoders to try for one format: H.264 all of ENCODER_ORDER (hardware,
        then software); H.265 / AV1 the hardware ones whose test encode passed."""
        if fmt == "h264":
            return list(ENCODER_ORDER)
        return (self._detection or codecs.UNKNOWN).encoders(fmt)

    def _plan_variants(self) -> list[_Variant]:
        wanted = self.cfg["capture"].get("encoder", "auto")
        if wanted not in ("", "auto"):
            names = [wanted]   # one named encoder: its format, whatever [capture] format says
        else:
            names = [n for fmt in (self._formats or ["h264"]) for n in self._format_encoders(fmt)]
        out = []
        for name in names:
            if not _have(name):
                continue
            if name in VA_ENCODERS and _have("vapostproc"):
                out.append(_Variant(name, True))
            out.append(_Variant(name, False))
        return out

    def _begin(self) -> None:
        self._restarted = False
        if self.source_name == "test":
            self._known_size = quality.source_size(tuple(self.test_size)) if self.test_size_known else None
        elif self.source_name != "portal":
            self._known_size = None  # x11 / gamescope: learnt from the first caps
        if self.source_name == "portal" and self._pw_fd is None:
            self._start_portal()
            return
        self._build_and_play()

    def _start_portal(self) -> None:
        from .portal import ScreenCastPortal

        if self._dbus is None:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop

            self._dbus = dbus.SessionBus(mainloop=DBusGMainLoop())
        from .portal import SOURCE_MONITOR, SOURCE_WINDOW

        target = "window" if self.window_mode else "screen"
        self._portal = ScreenCastPortal(
            self._dbus, config.portal_token_path(target), bool(self.cfg["capture"].get("show_cursor")),
            SOURCE_WINDOW if self.window_mode else SOURCE_MONITOR,
        )
        self._portal.start(self._on_portal_ready, self._on_portal_error)

    def _on_portal_ready(self, fd: int, node_id: int) -> None:
        if self._stop_requested:
            os.close(fd)
            return
        self._pw_fd, self._pw_node = fd, node_id
        # How big the stream is, so the pipeline is built at its final output size.
        self._known_size = self._portal_size_hint(getattr(self._portal, "stream_size", None))
        self._build_and_play()

    def _portal_size_hint(self, size) -> tuple[int, int] | None:
        """The portal's stream size, when it can be planned with; None: the first caps decide.

        It is only a hint. KDE announces a monitor's *logical* size (1600x900 for a
        1920x1080 screen at 120 % scaling) while the stream carries every pixel, so
        planning with it would pin the output below the real picture. A monitor's
        size is used only when it is the native mode of a connected display; any
        other (a scaled monitor, a mode that can't be read) is ignored and the
        first caps decide, as for a portal that says nothing. A window's size can't
        be checked this way and is used as announced (KDE doesn't send one); a
        wrong one is still caught by the first caps (``_pin_size``) and the
        renegotiation restart.
        """
        size = quality.source_size(size)
        if size is None or self.window_mode:
            return size
        if size in native_display_sizes():
            return size
        log.info("portal says the screen is %dx%d, not a connected display's own size (scaled?): "
                 "the stream's first frame decides the size", *size)
        return None

    def _on_portal_error(self, message: str) -> None:
        if self._stop_requested:
            self._close_portal()
            return
        if self.window_mode:
            if message == "cancelled":
                # The picker was dismissed: the stored window (if any) could not be restored.
                self._teardown(graceful=False)
                self._window_gone(WINDOW_NOT_PICKED, forget_token=True)
            elif message == "session closed":
                # The compositor ended the stream: the window is gone.
                self._teardown(graceful=self._got_fragment, source_lost=True)
                self._window_gone(WINDOW_CLOSED if self._got_fragment else WINDOW_NOT_PICKED,
                                  forget_token=True)
            else:
                self._teardown(graceful=False)
                self._close_portal()
                self._fatal(f"screen capture portal: {message}")  # no retry: it could open the picker
            return
        self._close_portal()
        self._teardown(graceful=False)
        if message == "cancelled":
            # The user dismissed the picker: do not nag them with a retry loop.
            self._stop_requested = True
            self._set_state("error", "screen capture permission was cancelled")
            return
        self._error_and_retry(f"screen capture portal: {message}")

    def _build_and_play(self) -> None:
        # A previous pipeline may have died mid-segment (error/retry): drop its
        # unfinished file and never reuse a number.
        self.ring.attach(self.buffer_dir)
        self._next_index = max(self._next_index, self.ring.recover())
        variant = self._variants[self._variant_idx]
        try:
            pipeline = self._build(variant)
        except (GLib.Error, RuntimeError) as e:
            log.warning("could not build pipeline with %s: %s", variant, e)
            self._next_variant_or_fail(str(e))
            return
        enc = pipeline.get_by_name("enc")
        self.encoder_name = enc.get_factory().get_name() if enc is not None else variant.encoder
        self.format_effective = codecs.format_of(self.encoder_name)
        self._variant = variant
        self._log_capture()
        self._pipeline = pipeline
        self._session = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self._params = None
        self._start_wall = None
        self._got_fragment = False
        self._open.clear()
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch = bus.connect("message", self._on_message)
        self._settle_from = time.monotonic()
        # AV1 / H.265: leave a marker until this start has proven stable, so a driver
        # that kills the process here is caught at the next daemon start (START_GUARD).
        self._cancel_stable()
        codecs.START_GUARD.begin(self.format_effective, self.encoder_name,
                                 getattr(self._detection, "key", None))
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._teardown(graceful=False)
            self._next_variant_or_fail(f"{variant}: failed to enter PLAYING")

    def _next_variant_or_fail(self, message: str, source_lost: bool = False) -> None:
        """Before any footage exists, fall back to the next encoder/conversion path.

        Moving on to another format (AV1 -> H.265 -> H.264) means the format failed
        to start: it is remembered for this process (codecs.DETECTOR.failed, so
        later starts skip it) and reported in ``format_fallback``. A failure of
        the video source is not the encoder's: it never changes the format.
        """
        if self._variant_idx + 1 < len(self._variants):
            cur = codecs.format_of(self._variants[self._variant_idx].encoder)
            nxt = codecs.format_of(self._variants[self._variant_idx + 1].encoder)
            if nxt != cur:
                if source_lost:
                    self._error_and_retry(message)
                    return
                codecs.DETECTOR.failed.add(cur)
                wanted = self._formats[0] if self._formats else cur
                self.format_fallback = (wanted, nxt)
                log.warning("%s didn't start (%s): recording in %s instead",
                            codecs.label(cur), message, codecs.label(nxt))
            self._variant_idx += 1
            log.info("falling back to %s", self._variants[self._variant_idx])
            GLib.idle_add(self._retry_build)
            return
        self._error_and_retry(message)

    def _retry_build(self) -> bool:
        if not self._stop_requested and self._pipeline is None:
            self._build_and_play()
        return False

    # --- pipeline description -----------------------------------------------------

    def _video_source(self) -> str:
        """The bare capture element; buffering is added by _video_chain."""
        cap = self.cfg["capture"]
        src = self.source_name
        if src == "test":
            # Named caps so a test can change the "window" size mid-stream.
            w, h = self.test_size
            hz = quality.refresh_hz(self.test_refresh)
            rate = f",max-framerate={round(hz)}/1" if hz else ""
            return ("videotestsrc name=src is-live=true pattern=ball ! "
                    f"capsfilter name=testcaps caps=video/x-raw,width={w},height={h}{rate}")
        if src == "x11":
            return f"ximagesrc name=src use-damage=false show-pointer={'true' if cap.get('show_cursor') else 'false'}"
        if src == "gamescope":
            return "pipewiresrc name=src target-object=gamescope min-buffers=4"
        if src == "portal":
            if self._pw_fd is None:
                raise RuntimeError("portal source without PipeWire fd")
            return f"pipewiresrc name=src fd={self._pw_fd} path={self._pw_node} min-buffers=4"
        raise RuntimeError(f"unknown capture source {src!r}")

    def _lock_size(self) -> bool:
        """Native resolution in window mode: the output keeps the first window size."""
        return self.size is None and self.window_mode

    def _plan_size(self, source: tuple[int, int] | None) -> None:
        """Decide what a source of size ``source`` (None: unknown) is recorded at.

        Sets ``source_size``, ``resolution_effective`` and ``_locked_size``: the
        output is pinned to ``quality.native_size`` of the source (its own size in
        even numbers, at most ``quality.MAX_HEIGHT`` lines) when the preset is
        taller than the source (never upscale), with ``native`` on a taller
        source, and in window mode with ``native`` (the size of a session never
        changes). Otherwise the preset's size (or, native, the source's own).
        """
        source = quality.source_size(source)
        self.source_size = source
        if source is None:
            self.resolution_effective = None
            self._locked_size = None
            return
        capped = self.size is not None and not quality.fits_source(self.size_name, source)
        shrink = self.size is None and source[1] > quality.MAX_HEIGHT  # native, taller than we record
        self.resolution_effective = "native" if capped else self.size_name
        pin = capped or shrink or self._lock_size()
        self._locked_size = quality.native_size(source) if pin else None

    def recorded_size(self) -> tuple[int, int] | None:
        """The picture size really recorded: the pinned or preset size, else (native) the
        source's own; None while that isn't known yet."""
        return self._locked_size or self.size or self.source_size

    def fps_why(self) -> str | None:
        """Why this frame rate, for fps "auto": "auto: 120 Hz screen" (None for a fixed rate)."""
        if self.fps_setting != "auto":
            return None
        hz = quality.hz_label(self.refresh_hz)
        return f"auto: {hz} Hz screen" if hz else "auto: screen refresh not known yet"

    def capture_summary(self) -> str:
        """The current (or last) capture, as its "recording:" log line says it."""
        v = self._variant
        cap = self.cfg["capture"]
        return describe_capture(
            self.recorded_size(), self.fps, str(cap.get("quality", quality.DEFAULT_QUALITY)).lower(), self._kbps,
            self.format_effective, self.encoder_name or (v.encoder if v else "?"), bool(v and v.zero_copy),
            self.window_mode, self.source_name, kbps_by_hand=int(cap.get("bitrate_kbps") or 0) > 0,
            fallback=self.format_fallback, stage=self._stage, fps_why=self.fps_why())

    def _log_key(self) -> tuple:
        """What the "recording:" line says that the first frame may change."""
        return self.recorded_size(), self._kbps, self.fps, self.fps_why()

    def _log_capture(self, note: str = "") -> None:
        """One line with everything that defines this recording (at every pipeline start,
        and again when the first frame changes its size, bitrate or frame rate)."""
        self._logged = self._log_key()
        log.info("recording: %s%s", self.capture_summary(), note)

    def _output_size(self) -> tuple[int, int] | None:
        """The size the encoder gets (None: whatever the source is, native on a screen)."""
        return self._locked_size or self.size

    def _output_caps(self) -> str:
        """The "size" capsfilter's caps for the current plan."""
        out = self._output_size()
        return self._size_caps + (f",width={out[0]},height={out[1]}" if out else "")

    def _rate_caps(self) -> str:
        """The "rate" capsfilter's caps (right after videorate) for the current frame rate."""
        return f"{self._rate_mem},framerate={self.fps}/1"

    def _log_size(self, kbps: int) -> None:
        w_h = self.source_size
        if w_h is None:
            return
        if self.resolution_effective == "native" and self.size is not None:
            log.info("source is %dx%d, smaller than %s: recording at %dx%d, %d kbps (never upscaled)",
                     *w_h, self.size_name, *self._locked_size, kbps)
        elif self.size is None and w_h[1] > quality.MAX_HEIGHT:
            log.info("source is %dx%d, taller than %dp: recording at %dx%d, %d kbps",
                     *w_h, quality.MAX_HEIGHT, *self._locked_size, kbps)
        elif self._locked_size is not None:
            log.info("window capture: output size locked to %dx%d for this session", *self._locked_size)

    def _video_chain(self, v: _Variant, tail: str | None = None) -> str:
        """The video chain after the source; ``tail`` replaces the encoder onwards (debug stages)."""
        tail = tail or self._encoder_tail(v)
        # Scaling keeps the aspect ratio; a screen of another shape gets black bars.
        # (A window that is resized mid-stream is scaled into the same frame.)
        # Always there: native may have to scale a picture taller than
        # quality.MAX_HEIGHT down (_pin_size); at the same size it passes through.
        scale = "videoscale add-borders=true ! "
        queue = "queue max-size-buffers={} max-size-bytes=0 max-size-time=0 leaky=downstream"
        # The output size lives in one named capsfilter ("size"), created at the
        # planned size (_plan_size); _pin_size changes it only when the source's
        # size wasn't known in advance (or turned out different).
        if v.zero_copy or v.encoder in VA_ENCODERS:
            self._size_caps = "video/x-raw(memory:VAMemory),format=NV12"
        elif v.encoder in ("x264enc", "openh264enc"):
            self._size_caps = "video/x-raw,format=I420"
        elif codecs.format_of(v.encoder) != "h264":
            # NVENC / Quick Sync H.265 and AV1 also take 10-bit input; keep them 8-bit
            # (a 10-bit stream may not play everywhere; the VA paths are NV12 as well).
            self._size_caps = "video/x-raw,format=NV12"
        else:
            self._size_caps = "video/x-raw"
        # The frame rate lives in one named capsfilter ("rate") right after videorate
        # ("vrate"): the first caps may change it (fps auto, _pin_size) without the
        # source renegotiating, since videorate takes any input rate.
        self._rate_mem = "video/x-raw(memory:VAMemory)" if v.zero_copy else "video/x-raw"
        rate = f'videorate name=vrate ! capsfilter name=rate caps="{self._rate_caps()}"'
        sized = f'capsfilter name=size caps="{self._output_caps()}"'
        if v.zero_copy:
            # Copy each frame into our own VA surface right away (GPU colour
            # conversion + scaling) so the compositor gets its buffer back within
            # a millisecond. KWin shares only 3-4 buffers; holding them in a
            # queue/videorate made it skip every other frame (~30 fps real motion).
            return f"vapostproc add-borders=true ! {sized} ! {queue.format(8)} ! {rate} ! {tail}"
        conv = f"{queue.format(3)} ! "
        if v.encoder in VA_ENCODERS:
            conv += f"videoconvert ! {rate} ! vapostproc add-borders=true ! {sized}"
        else:
            conv += f"videoconvert ! {scale}{rate} ! {sized}"
        return f"{conv} ! {tail}"

    def _encoder_tail(self, v: _Variant) -> str:
        first = f"{LOWPRIO_QUEUE} ! " if self._stage == "lowprio" else ""
        return f"{first}{v.encoder} name=enc ! {PARSERS[codecs.format_of(v.encoder)]} ! queue ! mux.video"

    def _audio_chain(self) -> str | None:
        a = self.cfg["audio"]
        test = self.source_name == "test"
        norm = "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"

        def src(device: str, freq: int, follow: str) -> str:
            if test:
                return f"audiotestsrc is-live=true wave=ticks freq={freq}"
            if device == follow and _have("pipewiresrc"):
                # A PipeWire stream with no target follows the default device,
                # so switching speakers/headphones/HDMI mid-session keeps working.
                # (pulsesrc resolves @DEFAULT_...@ once and stays pinned.)
                sink = ",stream.capture.sink=true" if follow == "@DEFAULT_MONITOR@" else ""
                return (f"pipewiresrc do-timestamp=true provide-clock=false "
                        f"stream-properties=\"props,node.name=momento-audio,node.description=Momento{sink}\" ! audio/x-raw")
            return f"pulsesrc device=\"{device}\" do-timestamp=true provide-clock=false"

        inputs = []
        if a.get("desktop"):
            inputs.append(src(a.get("desktop_device") or "@DEFAULT_MONITOR@", 440, "@DEFAULT_MONITOR@"))
        if a.get("microphone"):
            inputs.append(src(a.get("microphone_device") or "@DEFAULT_SOURCE@", 880, "@DEFAULT_SOURCE@"))
        if not inputs:
            return None
        aac = "avenc_aac" if _have("avenc_aac") else "fdkaacenc" if _have("fdkaacenc") else None
        if aac is None:
            log.warning("no AAC encoder (avenc_aac/fdkaacenc): recording without audio")
            return None
        tail = f"{norm} ! {aac} name=aenc ! aacparse ! queue ! mux.audio_0"
        if len(inputs) == 1:
            return f"{inputs[0]} ! queue ! {tail}"
        branches = " ".join(f"{i} ! queue ! {norm} ! amix." for i in inputs)
        return f"audiomixer name=amix ! {tail} {branches}"

    def _build(self, v: _Variant) -> Gst.Pipeline:
        seg_ns = int(float(self.cfg["buffer"]["segment_seconds"]) * Gst.SECOND)
        # The output size is decided before the chain is described, so the "size"
        # capsfilter starts at its final caps when the source's size is known.
        self._prepare_size()
        stage = self._stage = debug_stage()
        if stage == "capture":
            return self._build_capture_only(v)
        if stage in ("convert", "encode"):
            return self._build_partial(v, stage)
        if stage == "noaudio":
            log.warning("%s=noaudio: debug A/B. Recording normally but without the audio branch "
                        "(no audio source, AAC encoder or audio track)", STAGE_ENV)
        elif stage == "lowpower":
            cheap = (" ".join(f"{k.replace('_', '-')}={val}" for k, val in LOWPOWER_VA.items())
                     if v.encoder in VA_ENCODERS else f"nothing to change on {v.encoder}")
            log.warning("%s=lowpower: debug A/B. Recording normally with the encoder at its cheapest (%s)",
                        STAGE_ENV, cheap)
        elif stage == "lowbitrate":
            log.warning("%s=lowbitrate: debug A/B. Recording normally at %d%% of the bitrate "
                        "(%s at %d kbps instead of %d)", STAGE_ENV, round(LOWBITRATE_SHARE * 100),
                        v.encoder, round(self._kbps * LOWBITRATE_SHARE), self._kbps)
        elif stage == "lowprio":
            log.warning("%s=lowprio: debug A/B. Recording normally with the encoder made to yield: a leaky "
                        "2-frame queue before %s (frames drop instead of piling up) and its streaming thread at "
                        "SCHED_IDLE, nice %d (radeonsi's VA encoder has no lower-priority context)",
                        STAGE_ENV, v.encoder, LOWPRIO_NICE)
        # MPEG-TS for H.264 / H.265, Matroska for AV1 (codecs.CONTAINERS).
        muxer, suffix = codecs.container(codecs.format_of(v.encoder))
        parts = [
            f'splitmuxsink name=mux muxer="{codecs.muxer_description(muxer)}" send-keyframe-requests=true '
            "max-files=0 max-size-bytes=0",
            f"{self._video_source()} ! {self._video_chain(v)}",
        ]
        audio = None if stage == "noaudio" else self._audio_chain()
        if audio:
            parts.append(audio)
        pipeline = self._launch(" ".join(parts))

        # Recreate the folder if something removed it while we were running
        # (a cache cleaner, or a manual rm); otherwise every retry fails.
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        mux = pipeline.get_by_name("mux")
        mux.set_property("max-size-time", seg_ns)
        mux.set_property("location", str(self.buffer_dir / f"seg%08d{suffix}"))
        mux.set_property("start-index", self._next_index)

        self._setup_source(pipeline, v)

        enc = pipeline.get_by_name("enc")
        self._encoder_settings(enc, v.encoder, self._kbps)
        if stage == "lowpower" and v.encoder in VA_ENCODERS:
            _set(enc, **LOWPOWER_VA)
        elif stage == "vbr":
            mode = _vbr(enc) if v.encoder in VA_ENCODERS else None
            log.warning("%s=vbr: debug A/B. Recording normally with %s", STAGE_ENV,
                        f"{mode.upper()} rate control at the same target bitrate ({self._kbps} kbps)"
                        if mode else f"nothing to change on {v.encoder} (not a VA encoder)")
        elif stage == "lowprio":
            # The encoder thread can't be given its priority back (unprivileged,
            # RLIMIT_NICE 0): let idle threads end rather than be reused by other pools.
            GLib.ThreadPool.set_max_unused_threads(0)
            bus = pipeline.get_bus()
            bus.enable_sync_message_emission()
            bus.connect("sync-message::stream-status", _idle_encoder_thread)

        aenc = pipeline.get_by_name("aenc")
        if aenc is not None:
            _set(aenc, bitrate=int(self.cfg["audio"].get("bitrate_kbps", 160)) * 1000)

        # Pin the monotonic system clock so running-time -> wall-clock stays a
        # fixed offset (audio devices would otherwise provide a drifting clock).
        pipeline.use_clock(Gst.SystemClock.obtain())
        return pipeline

    def _setup_source(self, pipeline: Gst.Pipeline, v: _Variant) -> None:
        src = pipeline.get_by_name("src")
        if src.get_factory().get_name() == "pipewiresrc":
            # Resend the last frame on a static screen so the encoder (and
            # segment splitting) keeps going; error out when the stream dies.
            _set(src, keepalive_time=250, on_disconnect="error")
        # Sees the source's caps before they travel on: checks its size, and pins
        # the output size at runtime when it wasn't known in advance (fallback).
        vrate = pipeline.get_by_name("vrate")
        src.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._pin_size,
                                            (pipeline.get_by_name("size"), pipeline.get_by_name("enc"),
                                             v.encoder, pipeline.get_by_name("rate"),
                                             vrate.get_static_pad("sink") if vrate is not None else None))

    def _build_capture_only(self, v: _Variant) -> Gst.Pipeline:
        """``MOMENTO_DEBUG_STAGE=capture`` (``MOMENTO_DEBUG_CAPTURE_ONLY=1``): the source into a fakesink.

        No scaling, encoding, muxing or audio, and nothing is written. The source
        is offered the same formats the real chain's first element takes
        (vapostproc's, or system memory before videoconvert), so the compositor
        negotiates the same kind of buffers (DMA-BUF) as when recording.
        """
        log.warning("%s=capture (or %s=1): capture only, a debug A/B. The %s source feeds a fakesink: "
                    "no scaling, encoding, audio or recording, and saves find no new footage",
                    STAGE_ENV, CAPTURE_ONLY_ENV, self.source_name)
        pipeline = self._launch(f"{self._video_source()} ! capsfilter name=capture_caps ! {FAKESINK}")
        caps = None
        if v.zero_copy:
            factory = Gst.ElementFactory.find("vapostproc")
            for tmpl in factory.get_static_pad_templates() if factory else []:
                if tmpl.direction == Gst.PadDirection.SINK:
                    caps = tmpl.get_caps()
        else:
            caps = Gst.Caps.from_string("video/x-raw")
        if caps is not None:
            pipeline.get_by_name("capture_caps").set_property("caps", caps)
        self._setup_source(pipeline, v)
        pipeline.use_clock(Gst.SystemClock.obtain())
        return pipeline

    def _build_partial(self, v: _Variant, stage: str) -> Gst.Pipeline:
        """``MOMENTO_DEBUG_STAGE=convert|encode``: the real video chain cut short into a fakesink.

        convert stops after the conversion (vapostproc, the "size" caps, queue and
        videorate: everything before the encoder); encode keeps the encoder, with
        its normal settings. No parser, mux or audio, and nothing is written, so
        as with capture only the state still says "recording" and saves find no
        new footage.
        """
        what = ("the conversion (no encoder)" if stage == "convert"
                else f"the {v.encoder} encoder (no mux)")
        log.warning("%s=%s: debug A/B. The %s source runs through %s into a fakesink: "
                    "no audio or recording, and saves find no new footage",
                    STAGE_ENV, stage, self.source_name, what)
        tail = FAKESINK if stage == "convert" else f"{v.encoder} name=enc ! {FAKESINK}"
        pipeline = self._launch(f"{self._video_source()} ! {self._video_chain(v, tail)}")
        self._setup_source(pipeline, v)
        enc = pipeline.get_by_name("enc")
        if enc is not None:
            self._encoder_settings(enc, v.encoder, self._kbps)
        pipeline.use_clock(Gst.SystemClock.obtain())
        return pipeline

    @staticmethod
    def _launch(desc: str) -> Gst.Pipeline:
        log.debug("pipeline: %s", desc)
        pipeline = Gst.parse_launch(desc)
        if not isinstance(pipeline, Gst.Pipeline):
            raise RuntimeError("parse_launch did not return a pipeline")
        return pipeline

    def _encoder_settings(self, enc: Gst.Element, name: str, kbps: int) -> None:
        gop = self.fps
        if self._stage == "lowbitrate":
            kbps = round(kbps * LOWBITRATE_SHARE)
        if name in VA_ENCODERS:
            _set(enc, bitrate=kbps, key_int_max=gop, b_frames=0)
            if name == "vaav1enc":
                _set(enc, hierarchical_level=1)  # AV1's "no B-frames": no future references, no reordering
        elif name in ("vaapih264enc", "vaapih265enc"):
            _set(enc, bitrate=kbps, keyframe_period=gop, max_bframes=0)
        elif name.startswith(("nv", "qsv")):
            # NVENC / Quick Sync, any format (a property the element lacks is skipped)
            _set(enc, bitrate=kbps, gop_size=gop, bframes=0, b_frames=0)
        elif name == "x264enc":
            _set(enc, bitrate=kbps, key_int_max=gop, tune="zerolatency", speed_preset="veryfast", bframes=0)
        elif name == "openh264enc":
            _set(enc, bitrate=kbps * 1000, gop_size=gop)

    def _prepare_size(self) -> None:
        """Before a pipeline is built: plan its output size from the known source size
        (None: learnt from the first caps), its frame rate from the last known screen
        refresh, and the encoder bitrate that goes with them."""
        self._source_seen = False
        self._plan_size(self._known_size)
        self._plan_fps(None)
        # The bitrate of the size really recorded (unknown: the preset's, until the caps tell).
        self._kbps = quality.bitrate_kbps(self.cfg["capture"], self.source_size, self.refresh_hz)
        self._log_size(self._kbps)

    def _plan_fps(self, refresh) -> bool:
        """The frame rate for a screen of ``refresh`` Hz (None: the last one known stays).

        Sets ``refresh_hz`` (when ``refresh`` is known) and ``fps``: the setting, or for
        "auto" what that refresh records at. Returns whether ``fps`` changed.
        """
        refresh = quality.refresh_hz(refresh)
        if refresh is not None:
            self.refresh_hz = refresh
        old, self.fps = self.fps, quality.fps(self.cfg["capture"], self.refresh_hz)
        return self.fps != old

    def _caps_refresh(self, st: Gst.Structure) -> float | None:
        """The screen refresh (Hz) a video source's caps announce, None when they don't.

        KWin and Mutter screencasts say it as ``max-framerate`` (the output's refresh;
        ``framerate`` is 0/1, variable), wlroots' portal as ``framerate``. Only
        PipeWire sources (and the test source) count: another source's framerate is
        its own choice (x11), not the screen's.
        """
        if self.source_name not in ("portal", "gamescope", "test"):
            return None
        for field in ("max-framerate", "framerate"):
            ok, num, den = st.get_fraction(field)
            if ok and num > 0 and den > 0:
                return quality.refresh_hz(num / den)
        return None

    def _apply_rate_caps(self, rate, guard) -> bool:
        """Give the "rate" capsfilter the current frame rate, unless it has it already.

        ``guard`` is videorate's sink pad: the RECONFIGURE event the change sends
        upstream is dropped there, so the source (the compositor's stream) is not
        renegotiated; videorate converts from whatever rate comes in. Returns
        whether the caps changed.
        """
        if rate is None:
            return False
        want = Gst.Caps.from_string(self._rate_caps())
        have = rate.get_property("caps")
        if isinstance(have, Gst.Caps) and have.is_equal(want):
            return False
        probe = guard.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, _drop_reconfigure) if guard is not None else 0
        try:
            rate.set_property("caps", want)
        finally:
            if probe:
                guard.remove_probe(probe)
        return True

    def _pin_size(self, pad: Gst.Pad, info: Gst.PadProbeInfo, data: tuple):
        """Streaming thread: check the source's first caps against the plan.

        Normally the size was known when the pipeline was built (``_prepare_size``)
        and the "size" capsfilter already has the right caps: nothing changes.
        When the size wasn't known, or the first caps differ from what the portal
        said, the plan is made again from the real size and the capsfilter's caps
        are changed here, before the caps event travels on, and only if they
        differ (a fallback: on a PipeWire source it renegotiates with the
        compositor, see ``_may_be_renegotiation``). Later resizes (a window) are
        scaled into the session's size. The encoder gets the bitrate of the size
        really recorded.

        The frame rate (fps auto) is settled here too: the caps' refresh
        (``_caps_refresh``) picks it, and when that differs from the plan the
        "rate" capsfilter and the encoder's GOP and bitrate change, without the
        source renegotiating (``_apply_rate_caps``). ``data`` is (size capsfilter,
        encoder, encoder name[, rate capsfilter, videorate's sink pad]).
        """
        capsfilter, enc, encoder = data[:3]
        rate, guard = (data[3], data[4]) if len(data) > 4 else (None, None)
        event = info.get_event()
        if event is None or event.type != Gst.EventType.CAPS:
            return Gst.PadProbeReturn.OK
        caps = event.parse_caps()
        st = caps.get_structure(0)
        ok_w, w = st.get_int("width")
        ok_h, h = st.get_int("height")
        if not (ok_w and ok_h and w > 0 and h > 0):
            return Gst.PadProbeReturn.OK
        if not self._source_seen:
            self._source_seen = True
            # What the compositor announces (the frame rate fields tell the screen's
            # refresh), once per pipeline: the log says what fps auto was based on.
            log.info("source's first caps: %s", caps.to_string())
            refresh = self._caps_refresh(st)
            fps_changed = self._plan_fps(refresh)
            if fps_changed:
                self._apply_rate_caps(rate, guard)
                log.info("screen refresh %s Hz: recording at %d fps (fps auto; set by the first frame, "
                         "no restart)", quality.hz_label(refresh), self.fps)
            size_changed = self.source_size != (w, h)
            if size_changed:
                if self.source_size is not None:
                    log.info("source is %dx%d, not the %dx%d the portal announced", w, h, *self.source_size)
                self._plan_size((w, h))
                self._apply_output_caps(capsfilter)
            if size_changed or fps_changed:
                kbps = quality.bitrate_kbps(self.cfg["capture"], (w, h), self.refresh_hz)
                if enc is not None and (kbps != self._kbps or fps_changed):
                    self._kbps = kbps
                    self._encoder_settings(enc, encoder, kbps)   # the GOP follows the frame rate
                if size_changed:
                    self._log_size(kbps)
            if self._logged is not None and self._logged != self._log_key():
                self._log_capture(" (set by the first frame)")
        elif self._locked_size is not None and (w, h) != self._locked_size:
            log.info("source resized to %dx%d; scaled into %dx%d", w, h, *self._locked_size)
        else:
            refresh = self._caps_refresh(st)
            if (refresh is not None and self.fps_setting == "auto"
                    and quality.auto_fps(refresh) != self.fps):
                log.info("screen refresh now %s Hz: recording stays at %d fps until capture restarts",
                         quality.hz_label(refresh), self.fps)
        return Gst.PadProbeReturn.OK

    def _apply_output_caps(self, capsfilter) -> bool:
        """Give the "size" capsfilter the planned caps, unless it has them already.

        Returns whether they changed. A change is remembered (``_settle_from``):
        the source may renegotiate right after it.
        """
        if capsfilter is None:
            return False
        want = Gst.Caps.from_string(self._output_caps())
        have = capsfilter.get_property("caps")
        if isinstance(have, Gst.Caps) and have.is_equal(want):
            return False
        log.info("output caps changed after the stream started: %s", want.to_string())
        self._settle_from = time.monotonic()
        capsfilter.set_property("caps", want)
        return True

    # --- bus ------------------------------------------------------------------------

    def _wall(self, running_time: int) -> float:
        if self._start_wall is None:
            self._compute_start_wall()
        return self._start_wall + running_time / Gst.SECOND

    def _compute_start_wall(self) -> None:
        clock = self._pipeline.get_clock() if self._pipeline else None
        if clock is None:
            self._start_wall = time.time()
            return
        now_rt = clock.get_time() - self._pipeline.get_base_time()
        self._start_wall = time.time() - now_rt / Gst.SECOND

    def _on_message(self, bus: Gst.Bus, msg: Gst.Message) -> None:
        if self._pipeline is None or bus is not self._pipeline.get_bus():
            return
        t = msg.type
        if t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s is not None:
                self._on_element(s)
        elif t == Gst.MessageType.STATE_CHANGED:
            if msg.src is self._pipeline:
                _old, new, _pending = msg.parse_state_changed()
                if new == Gst.State.PLAYING and self._start_wall is None:
                    self._compute_start_wall()
                if new == Gst.State.PLAYING and not self.recording and self._pipeline.get_by_name("mux") is None:
                    # A debug stage without a mux: no segments will ever open; report it as running.
                    self.recording = True
                    self._arm_stable()
                    self._set_state("recording")
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            name = msg.src.get_name() if msg.src else "?"
            log.warning("pipeline error from %s: %s (%s)", name, err.message, dbg)
            # The video source failing means the captured stream went away (for a
            # window: it was closed).
            self._on_pipeline_failure(err.message, source_lost=name == "src")
        elif t == Gst.MessageType.EOS:
            log.warning("pipeline reached EOS unexpectedly")
            self._on_pipeline_failure("capture stream ended", source_lost=True)
        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            log.info("pipeline warning: %s (%s)", err.message, dbg)

    def _on_element(self, s: Gst.Structure) -> None:
        name = s.get_name()
        if name not in ("splitmuxsink-fragment-opened", "splitmuxsink-fragment-closed"):
            return
        location = s.get_string("location")
        ok, rt = s.get_uint64("running-time")
        if not location or not ok:
            return
        wall = self._wall(rt)
        if name == "splitmuxsink-fragment-opened":
            n = segment_number(location)
            if n is not None:
                self._next_index = n + 1
            self._open[location] = wall
            self.ring.opened(location, wall, session=self._session, **self._stream_params())
            log.debug("segment opened %s at %.3f", location, wall)
            if not self._got_fragment:
                self._got_fragment = True
                self.recording = True
                self._arm_stable()
                self._set_state("recording")
        else:
            start = self._open.pop(location, None)
            self.ring.closed(location, wall)
            log.debug("segment closed %s at %.3f (%.2fs)", location, wall, wall - start if start else -1)
            self._fire_flush_waiters(wall)
            if self._flush_waiters and self._pipeline is not None:
                # split-now cuts at the newest keyframe splitmuxsink already
                # holds, which can predate the request. Ask again: the next
                # keyframe is the one flush() forced, right after the request.
                self._pipeline.get_by_name("mux").emit("split-now")

    def _stream_params(self) -> dict:
        """Negotiated video size/frame rate (h264parse src, else encoder sink caps) + audio flag."""
        if self._params is not None:
            return self._params
        width = height = fps = None
        pipeline = self._pipeline
        for el_name, pad_name in (("parse", "src"), ("enc", "sink")):
            el = pipeline.get_by_name(el_name) if pipeline is not None else None
            pad = el.get_static_pad(pad_name) if el is not None else None
            caps = pad.get_current_caps() if pad is not None else None
            if caps is None or caps.get_size() == 0:
                continue
            st = caps.get_structure(0)
            ok_w, w = st.get_int("width")
            ok_h, h = st.get_int("height")
            if width is None and ok_w and ok_h and w > 0 and h > 0:
                width, height = w, h
            ok_f, num, den = st.get_fraction("framerate")
            if fps is None and ok_f and num > 0 and den > 0:
                fps = round(num / den, 3)
                fps = int(fps) if fps == int(fps) else fps
        if width is None and (self._locked_size or self.size):
            width, height = self._locked_size or self.size
        if fps is None:
            fps = self.fps
        enc = pipeline.get_by_name("enc") if pipeline is not None else None
        codec = codecs.format_of(enc.get_factory().get_name()) if enc is not None else "h264"
        params = {"width": width, "height": height, "fps": fps, "codec": codec,
                  "audio": pipeline is not None and pipeline.get_by_name("aenc") is not None}
        if width is not None:
            self._params = params  # caps are fixed for the life of this pipeline
        return params

    def _may_be_renegotiation(self) -> bool:
        """Could the video source's failure be a renegotiation rather than the stream ending?

        pipewiresrc (on-disconnect=error) reports "all buffers have been removed"
        both when the node is destroyed (the window closed) and when the
        compositor re-allocates its buffers because the format was renegotiated,
        e.g. right after the output caps changed. Within ``RENEGOTIATE_GRACE`` of
        the start or of such a change, while our portal session (so its node) is
        still there, it is taken for the latter, once per start.
        """
        if self._restarted or self.source_name not in ("portal", "gamescope"):
            return False
        if self.source_name == "portal" and (self._portal is None or self._pw_fd is None):
            return False  # the portal session is gone: so is the stream
        return time.monotonic() - self._settle_from < RENEGOTIATE_GRACE

    def _restart_stream(self, message: str) -> None:
        """Rebuild the pipeline on the same PipeWire stream, at the size now known."""
        self._restarted = True
        log.info("video source stopped %.1fs after starting (%s): taken for a renegotiation, "
                 "not a closed window; restarting once at %s", time.monotonic() - self._settle_from, message,
                 "%dx%d" % self.source_size if self.source_size else "an unknown size")
        self._teardown(graceful=self._got_fragment, source_lost=True)
        if self.source_size is not None:
            self._known_size = self.source_size  # the real size: no runtime change this time
        GLib.idle_add(self._retry_build)

    def _on_pipeline_failure(self, message: str, source_lost: bool = False) -> None:
        if self._stop_requested:
            return
        if source_lost and self._may_be_renegotiation():
            self._restart_stream(message)
            return
        if self.window_mode and (source_lost or self._got_fragment):
            # No automatic retry for a window: a new session could open the picker
            # again and again. Finish the segment being written first.
            self._teardown(graceful=self._got_fragment, source_lost=source_lost)
            if source_lost:
                self._window_gone(WINDOW_CLOSED, forget_token=self.source_name == "portal")
            else:
                # Not the window going away (e.g. the encoder failed): keep the
                # window's token, so play restores it without asking.
                self._close_portal()
                self._fatal(WINDOW_STOPPED)
                self._fire_flush_waiters()
            return
        self._teardown(graceful=False)
        if not self._got_fragment:
            # Never produced footage: most likely caps negotiation or encoder
            # init failed. Try the next path before calling it an error.
            if self._variant_idx + 1 < len(self._variants):
                self._next_variant_or_fail(message, source_lost=source_lost)
                return
        if self.source_name == "portal":
            # The screencast session is probably gone; get a fresh one.
            self._close_portal()
        self._error_and_retry(message)

    # --- flush bookkeeping ------------------------------------------------------------

    def _fire_flush_waiters(self, closed_end: float | None = None) -> None:
        remaining = []
        for waiter in self._flush_waiters:
            req, cb, tid = waiter
            if closed_end is None or closed_end >= req - FLUSH_TOLERANCE:
                if tid:
                    GLib.source_remove(tid)
                GLib.idle_add(_once(cb))
            else:
                remaining.append(waiter)
        self._flush_waiters = remaining

    def _flush_timeout(self, waiter: list) -> bool:
        if waiter in self._flush_waiters:
            log.warning("flush timed out; using segments closed so far")
            self._flush_waiters.remove(waiter)
            waiter[2] = 0
            _once(waiter[1])()
        return False

    # --- the crash guard ------------------------------------------------------------

    def _arm_stable(self) -> None:
        """Recording started: once it has run codecs.STABLE_SECONDS, the format is
        taken as stable here and the start marker goes."""
        self._cancel_stable()
        if self.format_effective not in codecs.GUARDED:
            return
        self._stable_id = GLib.timeout_add_seconds(codecs.STABLE_SECONDS, self._format_stable, self._session)

    def _format_stable(self, session: str | None) -> bool:
        self._stable_id = 0
        if session == self._session and self._pipeline is not None and self.recording:
            codecs.START_GUARD.clear()
            log.info("%s has recorded for %d s: stable here", codecs.label(self.format_effective),
                     codecs.STABLE_SECONDS)
        return False

    def _cancel_stable(self) -> None:
        if self._stable_id:
            GLib.source_remove(self._stable_id)
            self._stable_id = 0

    # --- teardown / retry ------------------------------------------------------------

    def _teardown(self, graceful: bool, source_lost: bool = False) -> None:
        # (The start marker stays: an error on the way to a driver abort must still
        # count. The next start replaces it, a clean stop removes it.)
        self._cancel_stable()
        pipeline = self._pipeline
        if pipeline is None:
            return
        self._pipeline = None
        bus = pipeline.get_bus()
        if self._bus_watch is not None:
            bus.disconnect(self._bus_watch)
            self._bus_watch = None
        bus.remove_signal_watch()
        if graceful:
            if source_lost:
                # A source that errored out never sends its EOS: end the video
                # branch right after it, so splitmuxsink can finish the segment.
                src = pipeline.get_by_name("src")
                pad = src.get_static_pad("src") if src is not None else None
                peer = pad.get_peer() if pad is not None else None
                if peer is not None:
                    peer.send_event(Gst.Event.new_eos())
            # EOS finalises the segment being written so it is usable.
            pipeline.send_event(Gst.Event.new_eos())
            deadline = time.monotonic() + STOP_TIMEOUT
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    log.warning("pipeline did not drain within %.0fs", STOP_TIMEOUT)
                    break
                msg = bus.timed_pop_filtered(
                    int(left * Gst.SECOND),
                    Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.ELEMENT,
                )
                if msg is None or msg.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
                    break
                s = msg.get_structure()
                if s is not None:
                    self._on_element(s)
        pipeline.set_state(Gst.State.NULL)
        self.recording = False
        self._fail_frame_waiters()

    def _close_portal(self) -> None:
        if self._portal is not None:
            self._portal.close()
            self._portal = None
        if self._pw_fd is not None:
            try:
                os.close(self._pw_fd)
            except OSError:
                pass
            self._pw_fd = None
            self._pw_node = None
            self._known_size = None  # the next portal session says its own size

    def _window_gone(self, message: str, forget_token: bool) -> None:
        """Window mode: capture ends here and stays off until the user picks again."""
        self._stop_requested = True
        self._cancel_retry()
        self._close_portal()
        if forget_token:
            # A window token restores only that window, which no longer exists.
            config.forget_portal_token("window")
        self.recording = False
        self._fire_flush_waiters()
        self._set_state("no_window", message)

    def _error_and_retry(self, message: str) -> None:
        if self.window_mode:
            # No automatic retry for a window (it could open the picker again and
            # again); the user resumes or picks a window from the bar.
            self._close_portal()
            self._fatal(message)
            self._fire_flush_waiters()
            return
        self.recording = False
        self._set_state("error", message)
        self._fire_flush_waiters()
        if not self._stop_requested:
            self._cancel_retry()
            self._retry_id = GLib.timeout_add_seconds(RETRY_SECONDS, self._retry)

    def _fatal(self, message: str) -> None:
        self._stop_requested = True
        self._cancel_retry()
        self.recording = False
        self._set_state("error", message)

    def _retry(self) -> bool:
        self._retry_id = 0
        if self._stop_requested or self._pipeline is not None:
            return False
        log.info("retrying capture")
        # Earlier footage stays; the new pipeline starts a new session. Planned
        # again, so a format that failed to start meanwhile is skipped.
        self._start_capture(self._detection or codecs.UNKNOWN)
        return False

    def _cancel_retry(self) -> None:
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0

    def _set_state(self, state: str, message: str | None = None) -> None:
        self.state = state
        try:
            self.on_state(state, message)
        except Exception:  # noqa: BLE001 - a UI callback must not kill capture
            log.exception("on_state callback failed")


def _drop_reconfigure(_pad: Gst.Pad, info: Gst.PadProbeInfo):
    """Upstream event probe: drop a RECONFIGURE (see Recorder._apply_rate_caps)."""
    event = info.get_event()
    if event is not None and event.type == Gst.EventType.RECONFIGURE:
        return Gst.PadProbeReturn.DROP
    return Gst.PadProbeReturn.OK


def _vbr(enc: Gst.Element) -> str:
    """vbr stage: the first of VBR_MODES the encoder's driver offers; returns the mode set."""
    for mode in VBR_MODES:
        Gst.util_set_object_arg(enc, "rate-control", mode)  # a mode the driver lacks is refused
        if enc.get_property("rate-control").value_nick == mode:
            break
    return enc.get_property("rate-control").value_nick


def _idle_encoder_thread(_bus: Gst.Bus, msg: Gst.Message) -> None:
    """lowprio stage, a sync bus handler: runs in the thread that is starting a task.

    When that is the "encq" queue's streaming thread (it runs the encoder), it is
    put at SCHED_IDLE and nice LOWPRIO_NICE (both only this thread: Linux applies
    them per thread).
    """
    kind, owner = msg.parse_stream_status()
    if kind != Gst.StreamStatusType.ENTER or owner is None or owner.get_name() != "encq":
        return
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, LOWPRIO_NICE)
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
    except OSError as e:
        log.info("lowprio: could not lower the encoder thread %d: %s", tid, e)
        return
    log.info("lowprio: encoder thread %d at SCHED_IDLE, nice %d", tid, LOWPRIO_NICE)


def _once(cb: Callable[[], None]) -> Callable[[], bool]:
    def run() -> bool:
        try:
            cb()
        except Exception:  # noqa: BLE001
            log.exception("flush callback failed")
        return False

    return run
