"""Turn a ring-buffer Selection into a standalone MP4 without re-encoding.

How the cut works (verified against gst-launch splitmuxsink/mpegtsmux output
with ffmpeg 8.1):

* The segments come from one continuous capture, so their MPEG-TS timestamps
  are already continuous. We byte-concatenate them with ffmpeg's ``concatf:``
  protocol (a list file, one URL per line) instead of the ``concat`` demuxer,
  which re-derives timestamps from probed per-file durations and, combined
  with input ``-ss``, emits ~1 s of audio pre-roll plus MP4 edit lists.
* The start offset is snapped to the nearest video keyframe inside the first
  segment and applied as an *output* ``-ss``. Stream copy therefore begins on a
  keyframe, audio is cut at the same instant, and no edit-list trickery is
  needed for players to start cleanly. The end point stays fixed ("now").
* ``-avoid_negative_ts make_zero`` puts the first packet at t=0.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import durations

log = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class ExportError(RuntimeError):
    pass


def output_path(cfg: dict, seconds: float, when: datetime | None = None) -> Path:
    """Unique output path from cfg["output"]; creates the directory."""
    when = when or datetime.now()
    out_dir = Path(cfg["output"]["dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = cfg["output"].get("filename") or "Momento_{date}_{time}_{length}.mp4"
    name = name.format(
        date=when.strftime("%Y-%m-%d"),
        time=when.strftime("%H-%M-%S"),
        length=durations.label(seconds),
    )
    name = name.replace("/", "_")
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    path = out_dir / name
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists():
        path = out_dir / f"{stem}_{n}{suffix}"
        n += 1
    return path


def _list_entry(path: Path) -> str:
    path = Path(path).resolve()
    s = str(path)
    if "\n" in s or "\r" in s:
        raise ExportError(f"segment path contains a newline: {s!r}")
    # "file:" stops ffmpeg from treating a ':' in the path as a protocol prefix.
    return "file:" + s


def write_list(segments, list_path: Path) -> None:
    list_path.write_text("".join(_list_entry(s.path) + "\n" for s in segments))


def _keyframe_times(path: Path) -> list[float]:
    """Video keyframe *decode* times in a TS file, relative to the file's start_time.

    DTS, not PTS: with stream copy, ffmpeg's output -ss compares each packet's
    decode timestamp, so with B-frames a PTS-based cut would land just after
    the keyframe and drop the whole GOP.
    """
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=start_time:packet=pts_time,dts_time,flags",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        start = float(data.get("format", {}).get("start_time", 0) or 0)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("keyframe probe of %s failed: %s", path, e)
        return []
    times = []
    for pkt in data.get("packets", []):
        ts = pkt.get("dts_time", pkt.get("pts_time"))
        if "K" in pkt.get("flags", "") and ts not in (None, "N/A"):
            times.append(float(ts) - start)
    return sorted(times)


def snap_offset(offset: float, keyframes: list[float]) -> float:
    """Nearest keyframe time to offset (0 if none known)."""
    if offset <= 0 or not keyframes:
        return 0.0 if offset <= 0 else offset
    return max(0.0, min(keyframes, key=lambda k: abs(k - offset)))


def export(selection, out_path: Path) -> Path:
    """Blocking: write selection to out_path (atomic). Raises ExportError."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not selection.segments:
        raise ExportError("empty selection")
    missing = [str(s.path) for s in selection.segments if not Path(s.path).exists()]
    if missing:
        raise ExportError(f"segment(s) missing from buffer: {', '.join(missing)}")

    end = selection.offset + selection.duration
    start = selection.offset
    if start > 0:
        start = snap_offset(start, _keyframe_times(selection.segments[0].path))
    length = max(0.1, end - start)

    tmp = out_path.with_name(f".{out_path.stem}.tmp{out_path.suffix}")
    list_path = out_path.with_name(f".{out_path.stem}.segments.txt")
    try:
        write_list(selection.segments, list_path)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", f"concatf:{list_path}"]
        if start > 0:
            # Tiny epsilon so float rounding never drops the keyframe itself.
            cmd += ["-ss", f"{max(0.0, start - 0.001):.3f}"]
        cmd += ["-t", f"{length:.3f}",
                "-map", "0:v?", "-map", "0:a?", "-dn", "-sn",
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", "-f", "mp4", str(tmp)]
        log.debug("export: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            raise ExportError(f"cannot run ffmpeg: {e}") from e
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise ExportError(proc.stderr.strip() or f"ffmpeg exited with {proc.returncode}")
        os.replace(tmp, out_path)
        return out_path
    finally:
        for p in (tmp, list_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
