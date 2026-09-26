"""Live pipeline test with the synthetic "test" source (no screen capture, no portal).

Runs with pytest, or standalone: python3 tests/test_pipeline_live.py
"""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # noqa: F401

    HAVE_GST = True
except (ImportError, ValueError):
    HAVE_GST = False

try:
    import pytest
except ImportError:  # standalone runner
    pytest = None

if pytest is not None:
    pytestmark = pytest.mark.skipif(not HAVE_GST, reason="GStreamer (PyGObject) not available")

RUN_SECONDS = 7


def run_recorder(tmp_path: Path) -> dict:
    from momento import config
    from momento.pipeline import Recorder
    from momento.ringbuffer import RingBuffer

    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["capture"].update(source="test", resolution="1080p", bitrate_kbps=4000)
    cfg["buffer"].update(segment_seconds=2, dir=str(tmp_path / "buffer"))

    ring = RingBuffer(max_seconds=3600)
    states = []
    loop = GLib.MainLoop()
    result = {"ring": ring, "states": states}

    rec = Recorder(cfg, ring, lambda s, m: states.append((s, m)))
    t0 = time.time()

    def do_flush():
        result["flush_requested"] = time.time()

        def flushed():
            result["flushed_at"] = time.time()
            loop.quit()

        rec.flush(flushed, timeout=5.0)
        return False

    def bail():
        result["timed_out"] = True
        loop.quit()
        return False

    GLib.timeout_add(RUN_SECONDS * 1000, do_flush)
    GLib.timeout_add((RUN_SECONDS + 10) * 1000, bail)
    rec.start()
    loop.run()
    result["t0"] = t0
    result["t_end"] = time.time()
    result["encoder"] = rec.encoder_name
    result["source"] = rec.source_name
    with ring._lock:
        result["segments"] = list(ring._segments)
    rec.stop()
    return result


def ffprobe_codecs(path: Path) -> list:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(s["codec_name"] for s in json.loads(out.stdout)["streams"])


def check(tmp_path: Path) -> dict:
    r = run_recorder(tmp_path)
    states = [s for s, _ in r["states"]]
    assert "error" not in states, r["states"]
    assert states[:2] == ["starting", "recording"], r["states"]
    assert not r.get("timed_out"), "flush callback never fired"
    assert r["source"] == "test"

    closed = [s for s in r["segments"] if s.closed]
    assert len(closed) >= 3, closed
    for seg in closed:
        assert seg.path.exists(), seg.path
        span = seg.end - seg.start
        assert 0 < span <= 4.0, (seg, span)
        assert r["t0"] - 1 <= seg.start <= r["t_end"], seg
        assert seg.end <= r["t_end"] + 0.5, seg
    for a, b in zip(closed, closed[1:]):
        assert abs(b.start - a.end) < 0.2, (a, b)  # contiguous
    # The flush must have closed a segment reaching (almost) the request time.
    assert closed[-1].end >= r["flush_requested"] - 0.5, (closed[-1], r["flush_requested"])

    if shutil.which("ffprobe"):
        codecs = ffprobe_codecs(closed[1].path)
        assert codecs == ["aac", "h264"], codecs
        size = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(closed[1].path)], capture_output=True, text=True).stdout.split()[0].strip(",")
        assert size == "1920,1080", size  # 720p test pattern scaled to the 1080p preset
    return r


def test_pipeline_live(tmp_path):
    check(tmp_path)


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.INFO)
    if not HAVE_GST:
        print("SKIP: GStreamer not available")
        sys.exit(0)
    with tempfile.TemporaryDirectory() as d:
        r = check(Path(d))
        print(f"encoder={r['encoder']} states={r['states']}")
        for s in r["segments"]:
            print(f"  {s.path.name} {s.start:.3f} -> {s.end if s.end is None else round(s.end, 3)}"
                  f" ({(s.end - s.start) if s.end else 0:.2f}s)")
        print(f"flush requested {r['flush_requested']:.3f}, flushed {r.get('flushed_at', 0):.3f}")
        print("OK")
