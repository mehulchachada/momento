"""Live pipeline test with the synthetic "test" source (no screen capture, no portal).

Runs with pytest, or standalone: python3 tests/test_pipeline_live.py
"""

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401
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


WINDOW_SECONDS = 4


def check_window(tmp_path: Path) -> dict:
    """Window mode with the test source standing in for a window: resized mid-stream
    at native resolution (the output keeps its first size), then the "window" goes
    away (the source errors out): capture stops in no_window, no retry, and the
    segment being written is finished and kept."""
    from gi.repository import Gst

    from momento import config
    from momento.pipeline import WINDOW_CLOSED, Recorder
    from momento.ringbuffer import RingBuffer

    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["capture"].update(source="test", target="window", resolution="native", bitrate_kbps=2000)
    cfg["audio"]["desktop"] = False
    cfg["buffer"].update(segment_seconds=1, dir=str(tmp_path / "wbuffer"))
    ring = RingBuffer(max_seconds=3600)
    states = []
    loop = GLib.MainLoop()
    rec = Recorder(cfg, ring, lambda s, m: states.append((s, m)))

    def resize():
        caps = Gst.Caps.from_string("video/x-raw,width=1001,height=701")
        rec._pipeline.get_by_name("testcaps").set_property("caps", caps)
        return False

    def close_window():
        src = rec._pipeline.get_by_name("src")
        err = GLib.Error.new_literal(Gst.ResourceError.quark(), "stream disconnected (simulated)", 0)
        src.post_message(Gst.Message.new_error(src, err, "test"))
        GLib.timeout_add(2000, lambda: (loop.quit(), False)[1])  # time for a (wrong) retry to show
        return False

    GLib.timeout_add(WINDOW_SECONDS * 500, resize)
    GLib.timeout_add(WINDOW_SECONDS * 1000, close_window)
    GLib.timeout_add((WINDOW_SECONDS + 10) * 1000, lambda: (loop.quit(), False)[1])
    rec.start()
    loop.run()
    result = {"states": list(states), "locked": rec._locked_size, "pipeline": rec._pipeline}
    with ring._lock:
        result["segments"] = [s for s in ring._segments if s.closed]
    rec.stop()

    states = result["states"]
    assert [s for s, _ in states] == ["starting", "recording", "no_window"], states
    assert states[-1][1] == WINDOW_CLOSED, states
    assert result["pipeline"] is None and rec._retry_id == 0, "window mode must not retry"
    assert result["locked"] == (1280, 720), result["locked"]
    segs = result["segments"]
    assert len(segs) >= 3, segs
    assert {(s.width, s.height) for s in segs} == {(1280, 720)}, segs  # never the resized size
    covered = sum(s.end - s.start for s in segs)
    assert covered >= WINDOW_SECONDS - 1.2, covered  # the last piece before the close is kept
    if shutil.which("ffprobe"):
        for seg in (segs[-1], segs[len(segs) // 2]):
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "frame=width,height", "-of", "csv=p=0", str(seg.path)],
                                 capture_output=True, text=True).stdout.split()
            assert out and set(out) == {"1280,720"}, (seg, set(out))
    return result


def test_window_mode_live(tmp_path):
    check_window(tmp_path)


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
        w = check_window(Path(d))
        print(f"window mode: states={[s for s, _ in w['states']]} locked={w['locked']} "
              f"segments={len(w['segments'])} ({sum(s.end - s.start for s in w['segments']):.2f}s kept)")
        print("OK")
