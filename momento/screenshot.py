"""Screenshots: one frame of the recording, saved as a PNG under <output dir>/Images.

The frame comes from the running capture pipeline (``Recorder.grab_frame``):
the picture the encoder receives, so it shows exactly what is recorded (the
picked window in window mode, the screen otherwise) at the recording's
resolution, before any video compression. It costs nothing until a
screenshot is taken: no extra pipeline branch runs while recording.

Converting it takes a tiny throwaway pipeline. A frame in GPU memory
(VAMemory, the zero-copy path) is copied to system memory by ``vapostproc`` on
the recording's own VA display, as NV12 (the RGBA download of some drivers
swaps channels), then ``videoconvert`` makes RGB and ``pngenc`` the file. Run
it off the main loop: about 60 ms for a 1080p frame, 0.2 s at 4K.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

IMAGES_DIR = "Images"                 # inside the clips folder (output.dir)
FILENAME = "Momento_{date}_{time}.png"
# pngenc 0-9. Measured on a 1080p photo: 3 takes 61 ms / 4.0 MB, the default 6
# 79 ms / 3.9 MB (4K: 221 vs 289 ms), 1 is no faster than 3.
PNG_COMPRESSION = 3
ENCODE_TIMEOUT = 10.0


class ScreenshotError(RuntimeError):
    pass


def images_dir(cfg: dict) -> Path:
    return Path(cfg["output"]["dir"]).expanduser() / IMAGES_DIR


def file_name(when: datetime) -> str:
    return FILENAME.format(date=when.strftime("%Y-%m-%d"), time=when.strftime("%H-%M-%S"))


def write_new(directory: Path, name: str, data: bytes) -> Path:
    """Write ``data`` as ``name`` in ``directory`` (created if needed), never over a file.

    A taken name gets ``_2``, ``_3``, ... before the extension. The data goes to a
    temporary file first and is linked in under the free name, so the picture
    never appears half written and a race with another writer can't overwrite it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".momento-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        stem, suffix = os.path.splitext(name)
        n = 1
        while True:
            path = directory / (name if n == 1 else f"{stem}_{n}{suffix}")
            try:
                os.link(tmp, path)
                return path
            except FileExistsError:
                n += 1
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def encode_png(frame) -> bytes:
    """PNG bytes of a ``pipeline.Frame`` (system or VA memory). Blocks; not for the main loop."""
    from gi.repository import Gst

    caps = frame.caps
    features = caps.get_features(0)
    va = features is not None and features.contains("memory:VAMemory")
    if features is not None and not va and not features.contains("memory:SystemMemory"):
        raise ScreenshotError(f"cannot read frames in {features.to_string()}")
    download = "vapostproc ! video/x-raw,format=NV12 ! " if va else ""
    desc = (f"appsrc name=in format=time ! {download}videoconvert ! video/x-raw,format=RGB ! "
            f"pngenc compression-level={PNG_COMPRESSION} ! appsink name=out sync=false")
    pipeline = Gst.parse_launch(desc)
    if va and frame.va_context is not None:
        pipeline.set_context(frame.va_context)  # read the surface on the display that made it
    src = pipeline.get_by_name("in")
    src.set_property("caps", caps)
    sink = pipeline.get_by_name("out")
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise ScreenshotError("could not start the image converter")
        src.emit("push-buffer", frame.buffer)
        src.emit("end-of-stream")
        sample = sink.emit("try-pull-sample", int(ENCODE_TIMEOUT * Gst.SECOND))
        if sample is None:
            msg = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            why = msg.parse_error()[0].message if msg is not None else "timed out"
            raise ScreenshotError(f"could not convert the frame: {why}")
        out = sample.get_buffer()
        ok, info = out.map(Gst.MapFlags.READ)
        if not ok:
            raise ScreenshotError("could not read the converted frame")
        try:
            return bytes(info.data)
        finally:
            out.unmap(info)
    finally:
        pipeline.set_state(Gst.State.NULL)


def save(frame, cfg: dict, when: datetime | None = None) -> Path:
    """Encode ``frame`` and write it as <output dir>/Images/Momento_<date>_<time>.png."""
    when = when or datetime.now()
    data = encode_png(frame)
    path = write_new(images_dir(cfg), file_name(when), data)
    log.info("screenshot saved: %s (%dx%d, %d kB)", path, *frame.size, len(data) // 1000)
    return path
