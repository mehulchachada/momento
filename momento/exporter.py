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

Per codec (``Segment.codec``): H.264 and H.265 segments are MPEG-TS, AV1 ones
Matroska written "streamable" (unknown sizes: ffmpeg reads byte-joined ones
through to the last, where sized ones stop after the first); the same
byte-concatenation and cut work for both. H.265 gets the
``hvc1`` sample entry in the MP4 (what Apple devices and browsers expect; ffmpeg
would write ``hev1``). Every clip is MP4 with the video and AAC audio copied.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from . import durations
from .ringbuffer import drop_cache

log = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Extra ffmpeg output options per codec, for the MP4.
MP4_OPTIONS = {"h265": ["-tag:v", "hvc1"]}


# ffmpeg runs below the game: CPU niceness 10, and the lowest best-effort I/O
# priority (class 2, level 7). Not the idle class: the recorder writes all the
# time, and with BFQ an idle-class reader can starve behind it.
NICE = 10
IOPRIO_CLASS_BE, IOPRIO_LEVEL = 2, 7
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30, "i386": 289, "i686": 289}  # syscall numbers
# How long a cancelled ffmpeg gets to exit after SIGTERM before SIGKILL.
KILL_GRACE_S = 2.0

# The files an export writes next to the clip while it runs: a hidden temp MP4
# (renamed to the clip's name only once complete), the segment list, and the
# per-session pieces. A crash can leave them behind; see clean_leftovers().
TEMP_RE = re.compile(r"^\..+\.(?:tmp\.mp4|segments\.txt|part\d+\.mp4)$")


class ExportError(RuntimeError):
    pass


class ExportCancelled(ExportError):
    """The export was stopped on request (Momento closing); its temp files are gone."""


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


def codec_options(segments) -> list[str]:
    """MP4_OPTIONS for the codec of ``segments`` (all one codec; unknown: H.264)."""
    codec = next((s.codec for s in segments if getattr(s, "codec", None)), None) or "h264"
    return list(MP4_OPTIONS.get(codec, []))


def _cut(segments, offset: float, duration: float, out: Path, fmt: str, list_path: Path,
         cancel: threading.Event | None = None) -> None:
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
        cmd += codec_options(segments) + ["-movflags", "+faststart"]
    cmd += ["-f", fmt, str(out)]
    _run(cmd, out, cancel)


def _join(pieces: list[Path], out: Path, list_path: Path, options=(), cancel: threading.Event | None = None) -> None:
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
           "-c", "copy", "-avoid_negative_ts", "make_zero", *options,
           "-movflags", "+faststart", "-f", "mp4", str(out)]
    _run(cmd, out, cancel)


def lower_priority(pid: int) -> None:
    """Put a running process below the game: nice NICE, best-effort I/O at IOPRIO_LEVEL.

    Set from outside after the start (no preexec_fn, which is unsafe in a threaded
    daemon). Silently skipped where the system doesn't allow it.
    """
    try:
        os.setpriority(os.PRIO_PROCESS, pid, NICE)
    except (OSError, AttributeError) as e:
        log.debug("cannot lower the CPU priority of %d: %s", pid, e)
    nr = _IOPRIO_SET.get(platform.machine())
    if nr is None:
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # ioprio_set(IOPRIO_WHO_PROCESS, pid, class << 13 | level)
        if libc.syscall(nr, 1, pid, (IOPRIO_CLASS_BE << 13) | IOPRIO_LEVEL) != 0:
            log.debug("cannot lower the I/O priority of %d: errno %d", pid, ctypes.get_errno())
    except (OSError, AttributeError) as e:
        log.debug("cannot lower the I/O priority of %d: %s", pid, e)


def _run(cmd: list[str], out: Path, cancel: threading.Event | None = None) -> None:
    """Run one ffmpeg step at low priority; stop it (and raise ExportCancelled) once ``cancel`` is set."""
    log.debug("export: %s", " ".join(cmd))
    if cancel is not None and cancel.is_set():
        raise ExportCancelled("cancelled")
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
    except OSError as e:
        raise ExportError(f"cannot run ffmpeg: {e}") from e
    lower_priority(proc.pid)
    stderr = ""
    try:
        while True:
            try:
                _out, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    _stop(proc)
                    raise ExportCancelled("cancelled") from None
    finally:
        if proc.poll() is None:   # an unexpected error on our side: never leave ffmpeg running
            _stop(proc)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise ExportError((stderr or "").strip() or f"ffmpeg exited with {proc.returncode}")


def _stop(proc) -> None:
    """SIGTERM, then SIGKILL after KILL_GRACE_S; reaps the process and closes its pipe."""
    try:
        proc.terminate()
        proc.communicate(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    except (OSError, ValueError):
        pass


def clean_leftovers(out_dir) -> list[Path]:
    """Delete the temp files of an export that never finished (a crash, a kill -9).

    Only hidden files named like an export's own temp files (TEMP_RE), directly in
    the clips folder. Returns what was removed. Call it only when no export runs.
    """
    removed = []
    try:
        entries = list(Path(out_dir).expanduser().iterdir())
    except OSError:
        return removed
    for p in entries:
        if TEMP_RE.match(p.name) and p.is_file() and not p.is_symlink():
            try:
                p.unlink()
                removed.append(p)
            except OSError as e:
                log.warning("cannot remove the leftover %s: %s", p, e)
    return removed


def check_compatible(runs) -> None:
    """Refuse to join sessions whose stream parameters differ (select_last never offers them)."""
    params = {r.segments[0].params() for r in runs}
    if len(params) > 1:
        raise ExportError("selection mixes footage with different video/audio parameters")


def export(selection, out_path: Path, cancel: threading.Event | None = None) -> Path:
    """Blocking: write selection to out_path (atomic). Raises ExportError.

    ``cancel``: once set, the running ffmpeg is stopped, the temp files are removed
    and ExportCancelled is raised; out_path never exists half-written (the clip is
    written to a hidden temp name and renamed when complete).

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
            _cut(runs[0].segments, runs[0].offset, runs[0].duration, tmp, "mp4", list_path, cancel)
        else:
            check_compatible(runs)
            pieces = []
            for i, run in enumerate(runs):
                if run.duration <= 0.05:
                    continue
                piece = out_path.with_name(f".{out_path.stem}.part{i}.mp4")
                temps.append(piece)
                _cut(run.segments, run.offset, run.duration, piece, "mp4", list_path, cancel)
                pieces.append(piece)
            if not pieces:
                raise ExportError("empty selection")
            if len(pieces) == 1:
                os.replace(pieces[0], tmp)
            else:
                _join(pieces, tmp, list_path, codec_options(selection.segments), cancel)
        if cancel is not None and cancel.is_set():
            raise ExportCancelled("cancelled")
        os.replace(tmp, out_path)
        return out_path
    finally:
        drop_cache([s.path for s in selection.segments])  # read once; do not keep them cached
        for p in temps:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
