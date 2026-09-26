"""GStreamer capture pipeline: screen + audio -> H.264/AAC -> MPEG-TS segments.

Segments are written by splitmuxsink into the buffer directory; every
fragment open/close is reported to the RingBuffer with wall-clock times, which
is what lets the exporter cut [now - X, now] out of the buffer later.

Everything here runs on the GLib main loop of the caller.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from . import config, quality  # noqa: E402
from .ringbuffer import RingBuffer  # noqa: E402

log = logging.getLogger("momento.pipeline")

Gst.init(None)

ENCODER_ORDER = ["vah264enc", "vah264lpenc", "vaapih264enc", "nvh264enc", "qsvh264enc", "x264enc", "openh264enc"]
# Encoders that take VAMemory NV12 straight from vapostproc (GPU colour conversion).
VA_ENCODERS = {"vah264enc", "vah264lpenc"}
RETRY_SECONDS = 3
STOP_TIMEOUT = 3.0
# A fragment that closes up to this long before a flush request still counts as
# covering it: the forced keyframe is stamped with its capture time, which is a
# few frames of pipeline latency behind the moment flush() was called.
FLUSH_TOLERANCE = 0.25


@dataclass(frozen=True)
class _Variant:
    encoder: str
    zero_copy: bool

    def __str__(self) -> str:
        return f"{self.encoder} ({'zero-copy vapostproc' if self.zero_copy else 'videoconvert'})"


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
        self.fps = quality.FPS
        self.size = quality.resolution(cfg["capture"])

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
        self._open: dict[str, float] = {}
        self._flush_waiters: list[list] = []  # [request_wall, callback, timeout_id]

    # --- public API -------------------------------------------------------------

    def start(self) -> None:
        if self._pipeline is not None or (self._portal is not None and not self._stop_requested):
            return
        self._stop_requested = False
        self._cancel_retry()
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.ring.reset()
        for f in self.buffer_dir.glob("*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
        self._next_index = 0
        self.source_name = resolve_source(self.cfg["capture"]["source"])
        self._variants = self._plan_variants()
        self._variant_idx = 0
        if not self._variants:
            self._fatal("no usable H.264 encoder found (tried: %s)" % ", ".join(ENCODER_ORDER))
            return
        self._set_state("starting")
        self._begin()

    def stop(self) -> None:
        self._stop_requested = True
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
        mux.emit("split-now")

    # --- startup ------------------------------------------------------------------

    def _plan_variants(self) -> list[_Variant]:
        wanted = self.cfg["capture"].get("encoder", "auto")
        names = ENCODER_ORDER if wanted in ("", "auto") else [wanted]
        out = []
        for name in names:
            if not _have(name):
                continue
            if name in VA_ENCODERS and _have("vapostproc"):
                out.append(_Variant(name, True))
            out.append(_Variant(name, False))
        return out

    def _begin(self) -> None:
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
        self._portal = ScreenCastPortal(
            self._dbus, config.STATE_DIR / "portal_token", bool(self.cfg["capture"].get("show_cursor"))
        )
        self._portal.start(self._on_portal_ready, self._on_portal_error)

    def _on_portal_ready(self, fd: int, node_id: int) -> None:
        if self._stop_requested:
            os.close(fd)
            return
        self._pw_fd, self._pw_node = fd, node_id
        self._build_and_play()

    def _on_portal_error(self, message: str) -> None:
        self._close_portal()
        if self._stop_requested:
            return
        self._teardown(graceful=False)
        if message == "cancelled":
            # The user dismissed the picker: do not nag them with a retry loop.
            self._stop_requested = True
            self._set_state("error", "screen capture permission was cancelled")
            return
        self._error_and_retry(f"screen capture portal: {message}")

    def _build_and_play(self) -> None:
        variant = self._variants[self._variant_idx]
        try:
            pipeline = self._build(variant)
        except (GLib.Error, RuntimeError) as e:
            log.warning("could not build pipeline with %s: %s", variant, e)
            self._next_variant_or_fail(str(e))
            return
        log.info("starting capture: source=%s encoder=%s", self.source_name, variant)
        self.encoder_name = variant.encoder
        self._pipeline = pipeline
        self._start_wall = None
        self._got_fragment = False
        self._open.clear()
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch = bus.connect("message", self._on_message)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._teardown(graceful=False)
            self._next_variant_or_fail(f"{variant}: failed to enter PLAYING")

    def _next_variant_or_fail(self, message: str) -> None:
        """Before any footage exists, fall back to the next encoder/conversion path."""
        if self._variant_idx + 1 < len(self._variants):
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
            return "videotestsrc name=src is-live=true pattern=ball ! video/x-raw,width=1280,height=720"
        if src == "x11":
            return f"ximagesrc name=src use-damage=false show-pointer={'true' if cap.get('show_cursor') else 'false'}"
        if src == "gamescope":
            return "pipewiresrc name=src target-object=gamescope min-buffers=4"
        if src == "portal":
            if self._pw_fd is None:
                raise RuntimeError("portal source without PipeWire fd")
            return f"pipewiresrc name=src fd={self._pw_fd} path={self._pw_node} min-buffers=4"
        raise RuntimeError(f"unknown capture source {src!r}")

    def _video_chain(self, v: _Variant) -> str:
        fps = f"{self.fps}/1"
        size = f",width={self.size[0]},height={self.size[1]}" if self.size else ""
        # Scaling keeps the aspect ratio; a screen of another shape gets black bars.
        scale = "videoscale add-borders=true ! " if self.size else ""
        queue = "queue max-size-buffers={} max-size-bytes=0 max-size-time=0 leaky=downstream"
        if v.zero_copy:
            # Copy each frame into our own VA surface right away (GPU colour
            # conversion + scaling) so the compositor gets its buffer back within
            # a millisecond. KWin shares only 3-4 buffers; holding them in a
            # queue/videorate made it skip every other frame (~30 fps real motion).
            return (f"vapostproc add-borders=true ! video/x-raw(memory:VAMemory),format=NV12{size} ! "
                    f"{queue.format(8)} ! videorate ! video/x-raw(memory:VAMemory),framerate={fps} ! "
                    f"{self._encoder_tail(v)}")
        conv = f"{queue.format(3)} ! "
        if v.encoder in VA_ENCODERS:
            conv += f"videoconvert ! videorate ! video/x-raw,framerate={fps} ! vapostproc add-borders=true ! video/x-raw(memory:VAMemory),format=NV12{size}"
        elif v.encoder in ("x264enc", "openh264enc"):
            conv += f"videoconvert ! {scale}videorate ! video/x-raw,format=I420,framerate={fps}{size}"
        else:
            conv += f"videoconvert ! {scale}videorate ! video/x-raw,framerate={fps}{size}"
        return f"{conv} ! {self._encoder_tail(v)}"

    @staticmethod
    def _encoder_tail(v: _Variant) -> str:
        return f"{v.encoder} name=enc ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream ! queue ! mux.video"

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
        parts = [
            "splitmuxsink name=mux muxer=mpegtsmux send-keyframe-requests=true max-files=0 max-size-bytes=0",
            f"{self._video_source()} ! {self._video_chain(v)}",
        ]
        audio = self._audio_chain()
        if audio:
            parts.append(audio)
        desc = " ".join(parts)
        log.debug("pipeline: %s", desc)
        pipeline = Gst.parse_launch(desc)
        if not isinstance(pipeline, Gst.Pipeline):
            raise RuntimeError("parse_launch did not return a pipeline")

        mux = pipeline.get_by_name("mux")
        mux.set_property("max-size-time", seg_ns)
        mux.set_property("location", str(self.buffer_dir / "seg%08d.ts"))
        mux.set_property("start-index", self._next_index)

        src = pipeline.get_by_name("src")
        if src.get_factory().get_name() == "pipewiresrc":
            # Resend the last frame on a static screen so the encoder (and
            # segment splitting) keeps going; error out when the stream dies.
            _set(src, keepalive_time=250, on_disconnect="error")

        kbps = quality.bitrate_kbps(self.cfg["capture"])
        enc = pipeline.get_by_name("enc")
        gop = self.fps
        name = v.encoder
        if name in VA_ENCODERS:
            _set(enc, bitrate=kbps, key_int_max=gop, b_frames=0)
        elif name == "vaapih264enc":
            _set(enc, bitrate=kbps, keyframe_period=gop, max_bframes=0)
        elif name in ("nvh264enc", "qsvh264enc"):
            _set(enc, bitrate=kbps, gop_size=gop, bframes=0, b_frames=0)
        elif name == "x264enc":
            _set(enc, bitrate=kbps, key_int_max=gop, tune="zerolatency", speed_preset="veryfast", bframes=0)
        elif name == "openh264enc":
            _set(enc, bitrate=kbps * 1000, gop_size=gop)

        aenc = pipeline.get_by_name("aenc")
        if aenc is not None:
            _set(aenc, bitrate=int(self.cfg["audio"].get("bitrate_kbps", 160)) * 1000)

        # Pin the monotonic system clock so running-time -> wall-clock stays a
        # fixed offset (audio devices would otherwise provide a drifting clock).
        pipeline.use_clock(Gst.SystemClock.obtain())
        return pipeline

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
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.warning("pipeline error from %s: %s (%s)", msg.src.get_name() if msg.src else "?", err.message, dbg)
            self._on_pipeline_failure(err.message)
        elif t == Gst.MessageType.EOS:
            log.warning("pipeline reached EOS unexpectedly")
            self._on_pipeline_failure("capture stream ended")
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
            m = re.search(r"(\d+)\.ts$", location)
            if m:
                self._next_index = int(m.group(1)) + 1
            self._open[location] = wall
            self.ring.opened(location, wall)
            log.debug("segment opened %s at %.3f", location, wall)
            if not self._got_fragment:
                self._got_fragment = True
                self.recording = True
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

    def _on_pipeline_failure(self, message: str) -> None:
        if self._stop_requested:
            return
        self._teardown(graceful=False)
        if not self._got_fragment:
            # Never produced footage: most likely caps negotiation or encoder
            # init failed. Try the next path before calling it an error.
            if self._variant_idx + 1 < len(self._variants):
                self._next_variant_or_fail(message)
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

    # --- teardown / retry ------------------------------------------------------------

    def _teardown(self, graceful: bool) -> None:
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

    def _error_and_retry(self, message: str) -> None:
        self.recording = False
        self._set_state("error", message)
        self._fire_flush_waiters()
        if not self._stop_requested:
            self._cancel_retry()
            self._retry_id = GLib.timeout_add_seconds(RETRY_SECONDS, self._retry)

    def _fatal(self, message: str) -> None:
        self._stop_requested = True
        self.recording = False
        self._set_state("error", message)

    def _retry(self) -> bool:
        self._retry_id = 0
        if self._stop_requested or self._pipeline is not None:
            return False
        log.info("retrying capture")
        # Footage before the gap is not contiguous with what comes next.
        self.ring.reset()
        self._variant_idx = 0
        self._set_state("starting")
        self._begin()
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


def _once(cb: Callable[[], None]) -> Callable[[], bool]:
    def run() -> bool:
        try:
            cb()
        except Exception:  # noqa: BLE001
            log.exception("flush callback failed")
        return False

    return run
