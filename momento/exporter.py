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

A selection spanning several capture sessions (pause/resume, restarts) cannot
be byte-concatenated: each session's timestamps start over. Each session's run
is cut as above into a temporary MP4 piece, and the pieces are joined with the
``concat`` demuxer (``-c copy``), which lays them back to back, so the gap
between sessions is skipped and each piece keeps its own A/V sync. Pieces must
share resolution, frame rate, codec and audio layout; the ring buffer only
selects such runs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import durations
from .ringbuffer import drop_cache

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


def _keyframe_times(path: Path, next_path: Path | None = None) -> list[float]:
    """Video keyframe *decode* times in a TS file, relative to the file's start_time.

    With next_path (the following segment of the same session), its first
    keyframe is included too, so an offset near the end of the first segment
    can snap forward to the segment boundary instead of back by up to a GOP.

    DTS, not PTS: with stream copy, ffmpeg's output -ss compares each packet's
    decode timestamp, so with B-frames a PTS-based cut would land just after
    the keyframe and drop the whole GOP.
    """
    start, times = _probe_keyframes(path)
    if start is None:
        return []
    if next_path is not None:
        _next_start, more = _probe_keyframes(next_path, first_only=True)
        times += more[:1]
    return sorted(t - start for t in times)


def _probe_keyframes(path: Path, first_only: bool = False) -> tuple[float | None, list[float]]:
    """(format start_time, absolute keyframe DTS times) of a TS file; (None, []) on failure."""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "format=start_time:packet=pts_time,dts_time,flags", "-of", "json"]
    if first_only:
        cmd += ["-read_intervals", "%+#90"]  # the first keyframe is the first packet
    try:
        out = subprocess.run(cmd + [str(path)], capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        start = float(data.get("format", {}).get("start_time", 0) or 0)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("keyframe probe of %s failed: %s", path, e)
        return None, []
    times = []
    for pkt in data.get("packets", []):
        ts = pkt.get("dts_time", pkt.get("pts_time"))
        if "K" in pkt.get("flags", "") and ts not in (None, "N/A"):
            times.append(float(ts))
    return start, sorted(times)


def snap_offset(offset: float, keyframes: list[float]) -> float:
    """Nearest keyframe time to offset (0 if none known)."""
    if offset <= 0 or not keyframes:
        return 0.0 if offset <= 0 else offset
    return max(0.0, min(keyframes, key=lambda k: abs(k - offset)))


def _cut(segments, offset: float, duration: float, out: Path, fmt: str, list_path: Path) -> None:
    """One session's segments -> out (stream copy), starting on the keyframe nearest offset."""
    end = offset + duration
    start = offset
    if start > 0:
        nxt = segments[1].path if len(segments) > 1 else None
        start = snap_offset(start, _keyframe_times(segments[0].path, nxt))
    length = max(0.1, end - start)
    write_list(segments, list_path)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", f"concatf:{list_path}"]
    if start > 0:
        # Tiny epsilon so float rounding never drops the keyframe itself.
        cmd += ["-ss", f"{max(0.0, start - 0.001):.3f}"]
    cmd += ["-t", f"{length:.3f}",
            "-map", "0:v?", "-map", "0:a?", "-dn", "-sn",
            "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if fmt == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", fmt, str(out)]
    _run(cmd, out)


def _join(pieces: list[Path], out: Path, list_path: Path) -> None:
    """Concatenate MP4 pieces (each starting at 0) back to back; the gaps between them vanish.

    The concat demuxer shifts every piece by the summed durations of the pieces
    before it, applied to audio and video alike, so each piece keeps its own A/V
    sync. Pieces are MP4 because their durations are exact (TS durations are
    estimated from the last timestamps).
    """
    lines = ["ffconcat version 1.0"]
    for p in pieces:
        s = str(Path(p).resolve())
        if "\n" in s or "\r" in s:
            raise ExportError(f"temp path contains a newline: {s!r}")
        lines.append("file '" + s.replace("'", "'\\''") + "'")
    list_path.write_text("\n".join(lines) + "\n")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-map", "0:v?", "-map", "0:a?", "-dn", "-sn",
           "-c", "copy", "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart", "-f", "mp4", str(out)]
    _run(cmd, out)


def _run(cmd: list[str], out: Path) -> None:
    log.debug("export: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise ExportError(f"cannot run ffmpeg: {e}") from e
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise ExportError(proc.stderr.strip() or f"ffmpeg exited with {proc.returncode}")


def check_compatible(runs) -> None:
    """Refuse to join sessions whose stream parameters differ (select_last never offers them)."""
    params = {r.segments[0].params() for r in runs}
    if len(params) > 1:
        raise ExportError("selection mixes footage with different video/audio parameters")


def export(selection, out_path: Path) -> Path:
    """Blocking: write selection to out_path (atomic). Raises ExportError.

    A selection from one capture session is cut in a single ffmpeg pass. One
    that spans several sessions (pause/resume, restarts) is cut per session and
    the pieces are joined back to back, so the time between sessions is skipped.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not selection.segments:
        raise ExportError("empty selection")
    missing = [str(s.path) for s in selection.segments if not Path(s.path).exists()]
    if missing:
        raise ExportError(f"segment(s) missing from buffer: {', '.join(missing)}")

    runs = selection.runs()
    tmp = out_path.with_name(f".{out_path.stem}.tmp{out_path.suffix}")
    list_path = out_path.with_name(f".{out_path.stem}.segments.txt")
    temps = [tmp, list_path]
    try:
        if len(runs) == 1:
            _cut(runs[0].segments, runs[0].offset, runs[0].duration, tmp, "mp4", list_path)
        else:
            check_compatible(runs)
            pieces = []
            for i, run in enumerate(runs):
                if run.duration <= 0.05:
                    continue
                piece = out_path.with_name(f".{out_path.stem}.part{i}.mp4")
                temps.append(piece)
                _cut(run.segments, run.offset, run.duration, piece, "mp4", list_path)
                pieces.append(piece)
            if not pieces:
                raise ExportError("empty selection")
            if len(pieces) == 1:
                os.replace(pieces[0], tmp)
            else:
                _join(pieces, tmp, list_path)
        os.replace(tmp, out_path)
        return out_path
    finally:
        drop_cache([s.path for s in selection.segments])  # read once; do not keep them cached
        for p in temps:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
