"""On-disk ring buffer of short, keyframe-aligned MPEG-TS segments.

The encoder writes a new segment every few seconds. Each closed segment is
appended to ``<buffer>/index.jsonl`` (file, wall-clock span, capture session and
stream parameters), so the buffer survives pauses, setting changes, service
restarts and reboots: on startup the index is reloaded and reconciled with the
directory (see ``recover()``).

Retention and selection work on *footage*, not on the wall clock: the buffer
keeps the newest segments whose summed duration covers ``max_seconds``, and
"save the last N seconds" walks segments newest -> oldest until it has N
seconds, skipping over the gaps between capture sessions (a pause, a restart).
A selection that spans several sessions is exported as one clip with the gaps
cut out (see ``exporter.py``). Segments that an export is reading are pinned so
pruning cannot delete them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_NAME = "index.jsonl"
# Stream parameters that must match for two sessions to be joined by stream copy.
PARAMS = ("width", "height", "fps", "codec", "audio")
_NUM_RE = re.compile(r"(\d+)\.ts$")
# Segments that closed at least this long before the newest one are dropped from
# the page cache (their dirty pages have been written back by then, so the
# advice takes effect without forcing an fsync).
CACHE_DROP_AGE = 30.0


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock seconds (time.time())
    end: float | None = None  # None while still being written
    session: str | None = None  # id of the capture run that wrote it
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    audio: bool | None = None
    pins: int = field(default=0, repr=False)
    cache_dropped: bool = field(default=False, repr=False)

    @property
    def closed(self) -> bool:
        return self.end is not None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start) if self.end is not None else 0.0

    def params(self) -> tuple:
        return tuple(getattr(self, k) for k in PARAMS)

    def to_json(self) -> str:
        return json.dumps({
            "file": self.path.name, "start": round(self.start, 6), "end": round(self.end, 6),
            "session": self.session, "width": self.width, "height": self.height,
            "fps": self.fps, "codec": self.codec, "audio": self.audio,
        }, separators=(",", ":"))


@dataclass
class Run:
    """Consecutive selected segments from one capture session (continuous timestamps)."""
    segments: list[Segment]
    offset: float  # seconds to skip into the first segment
    duration: float  # seconds of footage to keep


@dataclass
class Selection:
    segments: list[Segment]
    offset: float  # seconds to skip into the first segment
    duration: float  # seconds of footage to keep (sum over all runs)
    start: float  # wall-clock of the first kept frame
    end: float  # wall-clock of the last kept frame
    note: str | None = None  # why older footage was left out (e.g. a resolution change)

    def runs(self) -> list[Run]:
        """Split into runs of the same session; offsets/durations per run add up to self.duration."""
        groups: list[list[Segment]] = []
        for seg in self.segments:
            if groups and groups[-1][-1].session == seg.session:
                groups[-1].append(seg)
            else:
                groups.append([seg])
        runs, used = [], 0.0
        for i, group in enumerate(groups):
            offset = self.offset if i == 0 else 0.0
            if i == len(groups) - 1:
                length = self.duration - used
            else:
                length = sum(s.duration for s in group) - offset
            length = max(0.0, length)
            runs.append(Run(group, offset, length))
            used += length
        return runs


def _index_of(path: Path) -> int | None:
    m = _NUM_RE.search(path.name)
    return int(m.group(1)) if m else None


def _mismatch_reason(old: tuple, new: tuple) -> str:
    o = dict(zip(PARAMS, old))
    n = dict(zip(PARAMS, new))
    if (o["width"], o["height"]) != (n["width"], n["height"]):
        return "earlier footage used a different resolution"
    if o["fps"] != n["fps"]:
        return "earlier footage used a different frame rate"
    if o["audio"] != n["audio"]:
        return "earlier footage had no audio" if not o["audio"] else "earlier footage had audio"
    return "earlier footage used a different video format"


class RingBuffer:
    def __init__(self, max_seconds: float, margin: float = 30.0, directory: str | Path | None = None):
        self.max_seconds = max_seconds
        self.margin = margin
        self._segments: list[Segment] = []
        self._lock = threading.Lock()
        self.directory: Path | None = None
        self._loaded = False
        # Called with each segment right after it closes (the daemon counts the
        # footage of the current session with it). Runs where closed() is called:
        # the main loop, from the recorder.
        self.on_closed = None
        if directory is not None:
            self.attach(directory)

    # --- persistence ------------------------------------------------------------

    def attach(self, directory: str | Path) -> None:
        """Persist the index in directory (loaded on the next recover())."""
        directory = Path(directory)
        with self._lock:
            if self.directory == directory:
                return
            self.directory = directory
            self._loaded = False
            self._segments = [s for s in self._segments if s.pins]

    @property
    def index_path(self) -> Path | None:
        return self.directory / INDEX_NAME if self.directory is not None else None

    def recover(self) -> int:
        """Reconcile the index with the buffer directory; return the next free segment number.

        Loads the index on first use, drops entries whose file is gone or empty,
        forgets segments that were never closed (the capture died mid-write) and
        deletes any *.ts file the index does not know about, then rewrites the
        index. Call it while no pipeline is writing.
        """
        with self._lock:
            if self.directory is None:
                nums = [_index_of(s.path) for s in self._segments]
                return max([n for n in nums if n is not None], default=-1) + 1
            self.directory.mkdir(parents=True, exist_ok=True)
            if not self._loaded:
                self._segments = [s for s in self._segments if s.pins] + self._read_index()
                self._loaded = True
            keep = []
            for seg in self._segments:
                if seg.pins:
                    keep.append(seg)
                elif not seg.closed:
                    log.info("discarding unfinished segment %s", seg.path.name)
                    _unlink(seg.path)
                elif _size(seg.path) > 0:
                    keep.append(seg)
                else:
                    log.info("segment %s is missing or empty; dropped from the index", seg.path.name)
                    _unlink(seg.path)
            self._segments = keep
            known = {s.path.name for s in keep}
            highest = max([n for n in (_index_of(s.path) for s in keep) if n is not None], default=-1)
            for p in self.directory.glob("*.ts"):
                if p.name in known:
                    continue
                log.info("removing stray segment %s", p.name)
                if not _unlink(p):
                    n = _index_of(p)  # could not delete it: never reuse its number
                    if n is not None:
                        highest = max(highest, n)
            self._write_index()
        self.prune()
        return highest + 1

    def _read_index(self) -> list[Segment]:
        path = self.index_path
        out: list[Segment] = []
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            return out
        except OSError as e:
            log.warning("cannot read %s: %s", path, e)
            return out
        seen = set()
        for line in lines:
            try:
                d = json.loads(line)
                name = str(d["file"])
                if "/" in name or name in seen:
                    continue
                start, end = float(d["start"]), float(d["end"])
                seg = Segment(
                    self.directory / name, start, end, session=d.get("session"),
                    width=d.get("width"), height=d.get("height"), fps=d.get("fps"),
                    codec=d.get("codec"), audio=d.get("audio"),
                )
            except (ValueError, KeyError, TypeError):
                continue  # a torn last line after a crash
            seen.add(name)
            out.append(seg)
        return out

    def _write_index(self) -> None:
        """Rewrite the index atomically from the closed segments. Caller holds the lock."""
        path = self.index_path
        if path is None:
            return
        tmp = path.with_name(f".{INDEX_NAME}.tmp")
        try:
            with open(tmp, "w") as f:
                for seg in self._segments:
                    if seg.closed:
                        f.write(seg.to_json() + "\n")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("cannot write %s: %s", path, e)

    def _append_index(self, seg: Segment) -> None:
        path = self.index_path
        if path is None:
            return
        try:
            with open(path, "a") as f:
                f.write(seg.to_json() + "\n")
        except OSError as e:
            log.warning("cannot append to %s: %s", path, e)

    # --- fed by the recorder ------------------------------------------------

    def opened(self, path: str | Path, start: float, session: str | None = None, **params) -> None:
        seg = Segment(Path(path), start, session=session)
        for k in PARAMS:
            if k in params:
                setattr(seg, k, params[k])
        with self._lock:
            self._segments.append(seg)

    def closed(self, path: str | Path, end: float) -> None:
        path = Path(path)
        done = None
        with self._lock:
            for seg in reversed(self._segments):
                if seg.path == path and not seg.closed:
                    seg.end = end
                    self._append_index(seg)
                    done = seg
                    break
            cold = []
            for seg in self._segments:
                if seg.closed and not seg.cache_dropped and seg.end <= end - CACHE_DROP_AGE:
                    seg.cache_dropped = True
                    cold.append(seg.path)
        drop_cache(cold)
        self.prune()
        if done is not None and self.on_closed is not None:
            try:
                self.on_closed(done)
            except Exception:  # noqa: BLE001 - a listener must not break recording
                log.exception("segment listener failed")

    def clear(self) -> None:
        """Delete all footage and the index (explicit Stop). Pinned segments survive until released."""
        with self._lock:
            doomed = [s for s in self._segments if not s.pins]
            self._segments = [s for s in self._segments if s.pins]
            for seg in doomed:
                _unlink(seg.path)
            if self.directory is not None:
                for p in self.directory.glob("*.ts"):
                    if p not in {s.path for s in self._segments}:
                        _unlink(p)
                if self._segments:
                    self._write_index()
                else:
                    _unlink(self.index_path)

    # --- queries --------------------------------------------------------------

    # A segment still being written counts towards the live total for at most this
    # long, so a segment left open by a crash can't inflate the number forever.
    LIVE_SEGMENT_CAP = 30.0

    def buffered_seconds(self, live: bool = False, now: float | None = None) -> float:
        """Footage on disk (summed segment durations), capped at max_seconds.

        With ``live`` (set while recording), the segment currently being written
        counts too, so the total ticks every second instead of every segment.
        """
        with self._lock:
            total = sum(s.duration for s in self._segments if s.closed)
            if live and self._segments and not self._segments[-1].closed:
                now = time.time() if now is None else now
                total += min(max(0.0, now - self._segments[-1].start), self.LIVE_SEGMENT_CAP)
        return min(total, self.max_seconds)

    def latest_end(self) -> float | None:
        with self._lock:
            for seg in reversed(self._segments):
                if seg.closed:
                    return seg.end
        return None

    def select_last(self, seconds: float, until: float | None = None) -> Selection | None:
        """The newest `seconds` of footage (closed segments, across session gaps); pins them.

        Segments starting at or after `until` are ignored and the newest one is
        cut at `until`. If the sessions involved used different stream
        parameters, only the newest compatible tail is kept (Selection.note says
        why). Call release() after.
        """
        with self._lock:
            chosen: list[Segment] = []
            kept: list[float] = []
            total = 0.0
            note = None
            for seg in reversed(self._segments):
                if not seg.closed or (until is not None and seg.start >= until):
                    continue
                length = (min(seg.end, until) if until is not None else seg.end) - seg.start
                if length <= 0:
                    continue
                if chosen and seg.params() != chosen[-1].params():
                    # Stream copy cannot join these: keep the newest compatible tail.
                    note = _mismatch_reason(seg.params(), chosen[-1].params())
                    break
                chosen.insert(0, seg)
                kept.insert(0, length)
                total += length
                if total >= seconds:
                    break
            if not chosen:
                return None
            for seg in chosen:
                seg.pins += 1
        duration = min(total, seconds)
        offset = max(0.0, total - seconds)
        start = chosen[0].start + offset
        end = chosen[-1].start + kept[-1]
        return Selection(chosen, offset=offset, duration=duration, start=start, end=end, note=note)

    def select(self, since: float, until: float) -> Selection | None:
        """Closed segments overlapping the wall-clock window [since, until]; pins them.

        Kept for tools and tests; saves use select_last(). The window can span
        session gaps (they are skipped, so duration is the footage inside it).
        """
        with self._lock:
            chosen = [s for s in self._segments if s.closed and s.end > since and s.start < until]
            if not chosen:
                return None
            for seg in chosen:
                seg.pins += 1
        start = max(since, chosen[0].start)
        end = min(until, chosen[-1].end)
        footage = sum(min(s.end, until) - max(s.start, since) for s in chosen)
        return Selection(chosen, offset=start - chosen[0].start, duration=max(0.0, footage), start=start, end=end)

    def release(self, selection: Selection) -> None:
        with self._lock:
            for seg in selection.segments:
                seg.pins -= 1
        self.prune()

    # --- retention --------------------------------------------------------------

    def set_max_seconds(self, max_seconds: float) -> None:
        """Change the length kept (the Replay length setting), without a restart.

        Shorter: the oldest footage beyond it goes at once (the newest is kept;
        segments an export has pinned stay until it releases them). Longer: the
        ring grows from what is there.
        """
        self.max_seconds = max_seconds
        self.prune()

    def prune(self) -> None:
        """Delete the oldest footage beyond max_seconds (+ margin) and compact the index.

        A segment is kept while the footage newer than it is at most the limit,
        so the segment that straddles the limit stays and a full-length save is
        always possible.
        """
        limit = self.max_seconds + self.margin
        with self._lock:
            newer = 0.0
            doomed = []
            for seg in reversed(self._segments):
                if not seg.closed:
                    continue
                if newer > limit and not seg.pins:
                    doomed.append(seg)
                newer += seg.duration
            if not doomed:
                return
            gone = {id(s) for s in doomed}
            self._segments = [s for s in self._segments if id(s) not in gone]
            for seg in doomed:
                _unlink(seg.path)
            self._write_index()


def drop_cache(paths) -> None:
    """Ask the kernel to evict these files from the page cache (best effort, no fsync).

    A recorder writes gigabytes an hour that nobody reads back soon; without
    this the cache holds hundreds of MB of segments under memory pressure.
    """
    advise = getattr(os, "posix_fadvise", None)
    if advise is None:
        return
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            advise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        finally:
            os.close(fd)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unlink(path: Path | None) -> bool:
    """True when the file is gone afterwards."""
    if path is None:
        return True
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("cannot delete %s: %s", path, e)
        return False
    return True
