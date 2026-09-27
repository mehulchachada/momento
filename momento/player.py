"""Clip playback for the gallery: GStreamer, decoded and scaled on the GPU.

Why not QtMultimedia's QMediaPlayer + QVideoSink: with no QRhi on the sink (the
gallery paints the stage itself) every frame goes the long way. QVideoFrame.toImage()
copies the full-size frame out of the VA surface (vaGetImage), uploads it to a GL
texture, converts and reads it back, and QPainter then scales it down on the CPU.
Measured on the Z1 Extreme with a 1080p120 clip: about 1.2 cores, the GUI thread
busy 7-8 ms of every 8.3 ms frame, while receiving the frames alone costs 4 %.

Here playbin decodes (VA-API where there is a decoder: vah264dec, vaav1dec, ...),
``vapostproc`` scales to the picture's size in device pixels and converts to BGRA
on the GPU, and the gallery gets small QImages to draw 1:1: about a quarter of a
core for the same clip. Without VA, ``videoconvertscale`` does it on the CPU, still
straight to the picture's size. ``videorate`` drops frames above the display's
refresh rate before any of that. (playbin, not playbin3: playbin3 kept the whole of
an AV1 clip in memory here, 460 MB for a 3 min clip.)

``GstPlayer`` has the part of QMediaPlayer's surface the gallery uses (the signals,
play / pause / stop / setPosition / setSource, position / duration /
playbackState, setAudioOutput), plus ``frameReady(QImage)`` in place of a video
sink, ``set_video_size`` and ``set_max_rate``. There is no audio at all while muted
(no audio stream is decoded and no sound server stream exists); turning the sound on
rebuilds the pipeline at the same place.
"""

from __future__ import annotations

import enum
import logging
import os
import threading

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)

POSITION_MS = 100        # while playing: how often the position is reported
MIN_SIDE = 16            # the smallest picture asked of the scaler
# The audio sink when the sound is on, as a gst-launch description (None: playbin's
# autoaudiosink). Tests set "fakesink sync=true": nothing is ever played out loud.
AUDIO_SINK = None

# GstPlayFlags
_VIDEO, _AUDIO, _SOFT_VOLUME, _NATIVE_VIDEO = 0x1, 0x2, 0x10, 0x40


class PlaybackState(enum.IntEnum):
    StoppedState = 0
    PlayingState = 1
    PausedState = 2


class MediaStatus(enum.IntEnum):
    NoMedia = 0
    LoadingMedia = 1
    LoadedMedia = 2
    StalledMedia = 3
    BufferingMedia = 4
    BufferedMedia = 5
    EndOfMedia = 6
    InvalidMedia = 7


class Error(enum.IntEnum):
    NoError = 0
    ResourceError = 1
    FormatError = 2


_gst = None          # the Gst module once loaded, False when it can't be used
_gst_lock = threading.Lock()


def _load():
    global _gst
    if _gst is not None:
        return _gst or None
    with _gst_lock:
        if _gst is None:
            _gst = _init()
    return _gst or None


def _init():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
    except (ImportError, ValueError) as e:
        log.info("no GStreamer for the gallery (%s): it uses QtMultimedia", e)
        return False
    need = ("playbin", "appsink", "capsfilter", "videorate")
    scaler = Gst.ElementFactory.find("vapostproc") or Gst.ElementFactory.find("videoconvertscale")
    if scaler is None or not all(Gst.ElementFactory.find(n) for n in need):
        log.info("GStreamer lacks playbin / appsink / videorate / a scaler: the gallery uses QtMultimedia")
        return False
    return Gst


def ready() -> bool:
    """GStreamer's init has finished (or failed): ``available`` won't wait on it."""
    return _gst is not None


def available() -> bool:
    """GStreamer and the elements the player needs are there."""
    return _load() is not None


def warm_up():
    """GStreamer's init and the plugins the player loads (~100 ms together), done ahead,
    in any thread (the gallery does it while it lists the folder)."""
    Gst = _load()
    if Gst is None:
        return
    for name in ("playbin", "vapostproc", "videoconvertscale", "videorate", "appsink", "capsfilter"):
        if Gst.ElementFactory.find(name) is not None:
            Gst.ElementFactory.make(name)        # loads the plugin; the element is dropped


def display_rate(hz) -> int:
    """The most frames a second worth showing on a screen that refreshes at ``hz``
    (unknown or odd values: 60)."""
    try:
        hz = float(hz)
    except (TypeError, ValueError):
        return 60
    if not 20 <= hz <= 1000:
        return 60
    return int(round(hz))


def video_bin_description(gpu: bool, width: int, height: int, rate: int) -> str:
    """The video sink: drop above ``rate``, scale (letterboxed) to width x height, BGRA."""
    scale = "vapostproc add-borders=true" if gpu else "videoconvertscale add-borders=true"
    return (f"videorate name=rate drop-only=true max-rate={int(rate)} ! {scale} ! "
            f"capsfilter name=caps caps={caps_string(width, height)} ! "
            "appsink name=sink sync=true max-buffers=1 drop=true emit-signals=true enable-last-sample=false")


def caps_string(width: int, height: int) -> str:
    return (f"video/x-raw,format=BGRA,width={max(MIN_SIDE, int(width))},"
            f"height={max(MIN_SIDE, int(height))},pixel-aspect-ratio=1/1")


class GstPlayer(QObject):
    """A clip player with QMediaPlayer's shape (the part the gallery uses), frames as QImages."""

    PlaybackState = PlaybackState
    MediaStatus = MediaStatus
    Error = Error

    positionChanged = Signal(object)
    durationChanged = Signal(object)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)
    frameReady = Signal(object)          # QImage (RGB32, opaque, at the asked size)

    _frame_waiting = Signal()            # from the streaming thread: a frame is pending
    _bus_message = Signal(object)        # from any thread: (gen, kind, data)

    def __init__(self, parent=None):
        super().__init__(parent)
        Gst = _load()
        if Gst is None:
            raise RuntimeError("GStreamer is not usable")
        self.Gst = Gst
        self._lock = threading.Lock()
        self._pending = None             # (gen, QImage) handed from the streaming thread
        self._gen = 0                    # bumped on every source change / stop
        self._uri = None
        self._state = PlaybackState.StoppedState
        self._status = MediaStatus.NoMedia
        self._pos = 0                    # ms
        self._dur = 0                    # ms
        self._audio = False
        self._size = (640, 360)
        self._rate = 60
        self._prerolled = False          # the pipeline reached PAUSED since it left NULL
        self._seek_to = None             # ms, applied once prerolled
        self._at_end = False
        self._closed = False
        self._gpu = Gst.ElementFactory.find("vapostproc") is not None
        self.pipeline = None
        self._build()
        self._frame_waiting.connect(self._take_frame, Qt.QueuedConnection)
        self._bus_message.connect(self._on_message, Qt.QueuedConnection)
        self._clock = QTimer(self)
        self._clock.setInterval(POSITION_MS)
        self._clock.timeout.connect(self._report_position)

    # ------------------------------------------------------------ pipeline
    def _build(self):
        Gst = self.Gst
        pb = Gst.ElementFactory.make("playbin", "momento-gallery-player")
        vbin = Gst.parse_bin_from_description(video_bin_description(self._gpu, *self._size, self._rate), True)
        self._caps = vbin.get_by_name("caps")
        self._rate_el = vbin.get_by_name("rate")
        sink = vbin.get_by_name("sink")
        self._sink = sink
        self._sink_ids = [sink.connect("new-sample", self._on_sample, False),
                          sink.connect("new-preroll", self._on_sample, True)]
        pb.set_property("video-sink", vbin)
        self._set_flags(pb)
        bus = pb.get_bus()
        bus.set_sync_handler(self._on_bus)
        self.pipeline = pb

    def _set_flags(self, pb):
        """Audio off: no audio flag, and a fakesink as the audio sink, else playbin still makes
        (and opens) every audio sink it knows while plugging, to ask what they accept. Audio
        on: the default sink (AUDIO_SINK, or a fakesink under the tests' sandbox)."""
        flags = _VIDEO | _SOFT_VOLUME | _NATIVE_VIDEO | (_AUDIO if self._audio else 0)
        pb.set_property("flags", flags)
        if not self._audio:
            sink = "fakesink sync=true"
        else:
            sink = AUDIO_SINK
            if sink is None and os.environ.get("MOMENTO_TEST_SANDBOX"):
                sink = "fakesink sync=true"       # under the tests' sandbox nothing plays out loud
        pb.set_property("audio-sink", self.Gst.parse_launch(sink) if sink else None)

    def _to_null(self):
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)
        self._prerolled = False
        with self._lock:
            self._pending = None

    def close(self):
        """Stop and let go of the pipeline for good (breaks the Python <-> GStreamer cycles)."""
        if self._closed:
            return
        self._closed = True
        self._clock.stop()
        self._to_null()
        pb, self.pipeline = self.pipeline, None
        if pb is not None:
            pb.get_bus().set_sync_handler(None)
            for hid in self._sink_ids:
                self._sink.disconnect(hid)
            pb.set_property("video-sink", None)
        self._sink_ids, self._sink, self._caps, self._rate_el = [], None, None, None

    # ------------------------------------------------------------ streaming threads
    def _on_sample(self, sink, preroll):
        Gst = self.Gst
        sample = sink.emit("pull-preroll" if preroll else "pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        gen = self._gen
        try:
            s = sample.get_caps().get_structure(0)
            w, h = s.get_value("width"), s.get_value("height")
            buf = sample.get_buffer()
            stride, offset = w * 4, 0
            meta = _video_meta(buf)
            if meta is not None:
                stride, offset = meta.stride[0], meta.offset[0]
            data = buf.extract_dup(offset, stride * h)
            if len(data) < stride * h:
                return Gst.FlowReturn.OK
            img = QImage(data, w, h, stride, QImage.Format_RGB32)   # BGRA, alpha 255: opaque; keeps ``data``
        except Exception:  # noqa: BLE001 - a bad frame is skipped, playback goes on
            log.debug("frame skipped", exc_info=True)
            return Gst.FlowReturn.OK
        with self._lock:
            first = self._pending is None
            self._pending = (gen, img)       # a newer frame replaces one the GUI hasn't taken
        if first:
            self._frame_waiting.emit()
        return Gst.FlowReturn.OK

    def _on_bus(self, bus, msg, *_):
        Gst = self.Gst
        t = msg.type
        item = None
        if t == Gst.MessageType.ASYNC_DONE:
            item = ("async-done", None)
        elif t == Gst.MessageType.EOS:
            item = ("eos", None)
        elif t == Gst.MessageType.DURATION_CHANGED:
            item = ("duration", None)
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            item = ("error", (err.domain if err else "", err.message if err else "", dbg or ""))
        if item is not None:
            self._bus_message.emit((self._gen,) + item)
        return Gst.BusSyncReply.DROP

    # ------------------------------------------------------------ GUI thread
    def _take_frame(self):
        with self._lock:
            item, self._pending = self._pending, None
        if item is not None and item[0] == self._gen and not self._closed:
            self.frameReady.emit(item[1])

    def _on_message(self, m):
        gen, kind, data = m
        if gen != self._gen or self._closed:
            return
        if kind == "async-done":
            first = not self._prerolled
            self._prerolled = True
            if first:
                self._query_duration()
                if self._status in (MediaStatus.LoadingMedia, MediaStatus.NoMedia):
                    self._set_status(MediaStatus.LoadedMedia)
            if self._seek_to is not None:
                ms, self._seek_to = self._seek_to, None
                self._seek(ms)
            else:
                self._report_position()
        elif kind == "duration":
            self._query_duration()
        elif kind == "eos":
            self._at_end = True
            self._clock.stop()
            if self._dur:
                self._pos = self._dur
                self.positionChanged.emit(self._pos)
            self.pipeline.set_state(self.Gst.State.PAUSED)     # stays on the last frame
            self._set_status(MediaStatus.EndOfMedia)
            self._set_state(PlaybackState.StoppedState)
        elif kind == "error":
            domain, text, dbg = data
            log.warning("cannot play %s: %s", self._uri, text)
            log.debug("%s", dbg)
            self._clock.stop()
            self._to_null()
            self._set_status(MediaStatus.InvalidMedia)
            self._set_state(PlaybackState.StoppedState)
            fmt = "stream" in str(domain) or "core" in str(domain)
            self.errorOccurred.emit(Error.FormatError if fmt else Error.ResourceError, text)

    def _query_duration(self):
        ok, ns = self.pipeline.query_duration(self.Gst.Format.TIME)
        if ok and ns > 0:
            ms = int(ns // 1_000_000)
            if ms != self._dur:
                self._dur = ms
                self.durationChanged.emit(ms)

    def _report_position(self):
        if self.pipeline is None or not self._prerolled or self._seek_to is not None:
            return
        ok, ns = self.pipeline.query_position(self.Gst.Format.TIME)
        if ok and ns >= 0:
            ms = int(ns // 1_000_000)
            if self._dur:
                ms = min(ms, self._dur)
            if ms != self._pos:
                self._pos = ms
                self.positionChanged.emit(ms)

    def _set_state(self, state):
        if state != self._state:
            self._state = state
            self.playbackStateChanged.emit(state)

    def _set_status(self, status):
        if status != self._status:
            self._status = status
            self.mediaStatusChanged.emit(status)

    def _seek(self, ms):
        Gst = self.Gst
        self._at_end = False
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        if not self.pipeline.seek_simple(Gst.Format.TIME, flags, max(0, int(ms)) * 1_000_000):
            log.debug("seek to %d ms refused", ms)

    # ------------------------------------------------------------ QMediaPlayer's surface
    def setSource(self, url):
        self._clock.stop()
        self._to_null()
        self._gen += 1
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url or "")
        self._uri = self.Gst.filename_to_uri(path) if path else None
        self._pos, self._seek_to, self._at_end = 0, None, False
        if self._dur:
            self._dur = 0
            self.durationChanged.emit(0)
        self._set_state(PlaybackState.StoppedState)
        if self._uri is None:
            self._set_status(MediaStatus.NoMedia)
            return
        self.pipeline.set_property("uri", self._uri)
        self._set_status(MediaStatus.LoadingMedia)
        self.pipeline.set_state(self.Gst.State.PAUSED)          # preroll: the length, a first frame

    def setVideoSink(self, sink):   # noqa: N802 - QMediaPlayer's name; frames come as frameReady
        pass

    def setAudioOutput(self, audio):   # noqa: N802
        on = audio is not None
        if on == self._audio:
            return
        self._audio = on
        if self._closed:
            return
        if self._uri is None:
            self._set_flags(self.pipeline)
            return
        # Audio streams are chosen when the pipeline starts: rebuild it at the same place.
        state, pos = self._state, self._pos
        self._to_null()
        self._set_flags(self.pipeline)
        self.pipeline.set_state(self.Gst.State.PAUSED)
        if pos and not self._at_end:
            self._seek_to = pos
        if state == PlaybackState.PlayingState:
            self.pipeline.set_state(self.Gst.State.PLAYING)

    def audio_output(self, parent=None):
        """What the gallery hands to setAudioOutput to turn the sound on: a token, no device."""
        return QObject(parent)

    def play(self):
        if self._uri is None or self._closed:
            return
        if self._at_end and self._seek_to is None:
            self.setPosition(0)                # again from the start, like QMediaPlayer
        self._at_end = False
        if self._status == MediaStatus.EndOfMedia:
            self._set_status(MediaStatus.LoadedMedia)
        self.pipeline.set_state(self.Gst.State.PLAYING)
        self._set_state(PlaybackState.PlayingState)
        self._clock.start()

    def pause(self):
        if self._uri is None or self._closed:
            return
        self.pipeline.set_state(self.Gst.State.PAUSED)
        self._clock.stop()
        self._report_position()
        self._set_state(PlaybackState.PausedState)

    def stop(self):
        self._clock.stop()
        if self._closed:
            return
        self._to_null()
        self._gen += 1
        self._pos, self._seek_to, self._at_end = 0, None, False
        if self._status == MediaStatus.EndOfMedia:
            self._set_status(MediaStatus.LoadedMedia)
        self._set_state(PlaybackState.StoppedState)

    def setPosition(self, ms):   # noqa: N802
        ms = max(0, int(ms))
        if self._dur:
            ms = min(ms, self._dur)
        self._pos = ms
        if self._uri is not None and not self._closed:
            if self._prerolled:
                self._seek(ms)
            else:
                self._seek_to = ms
                if self._state == PlaybackState.StoppedState:
                    self.pipeline.set_state(self.Gst.State.PAUSED)
        self.positionChanged.emit(ms)

    def position(self):
        return self._pos

    def duration(self):
        return self._dur

    def playbackState(self):   # noqa: N802
        return self._state

    def mediaStatus(self):   # noqa: N802
        return self._status

    # ------------------------------------------------------------ the picture
    def set_video_size(self, width, height):
        """Frames come scaled (letterboxed) to width x height device pixels. While paused
        the current frame is made again at the new size."""
        size = (max(MIN_SIDE, int(width)), max(MIN_SIDE, int(height)))
        if size == self._size or self._closed:
            return
        self._size = size
        self._caps.set_property("caps", self.Gst.Caps.from_string(caps_string(*size)))
        if self._prerolled and self._state != PlaybackState.PlayingState:
            self._seek(self._pos)

    def set_max_rate(self, fps):
        rate = display_rate(fps)
        if rate != self._rate and not self._closed:
            self._rate = rate
            self._rate_el.set_property("max-rate", rate)

    @property
    def video_size(self):
        return self._size

    @property
    def max_rate(self):
        return self._rate


_GstVideo = None


def _video_meta(buf):
    """The buffer's GstVideoMeta (its real stride and offset), if it has one."""
    global _GstVideo
    if _GstVideo is None:
        try:
            import gi

            gi.require_version("GstVideo", "1.0")
            from gi.repository import GstVideo

            _GstVideo = GstVideo
        except (ImportError, ValueError):
            _GstVideo = False
    if not _GstVideo:
        return None
    return _GstVideo.buffer_get_video_meta(buf)
