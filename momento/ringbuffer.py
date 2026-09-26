"""On-disk ring buffer of short, keyframe-aligned MPEG-TS segments.

The encoder writes a new segment every few seconds. We remember the wall-clock
span of each one, delete segments that fall out of the retention window, and on
"save the last N seconds" hand back the segments that cover [now - N, now].
Segments that an export is reading are pinned so pruning cannot delete them.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock seconds (time.time())
    end: float | None = None  # None while still being written
    pins: int = field(default=0, repr=False)

    @property
    def closed(self) -> bool:
        return self.end is not None


@dataclass
class Selection:
    segments: list[Segment]
    offset: float  # seconds to skip into the first segment
    duration: float  # seconds of footage to keep
    start: float  # wall-clock of the first kept frame
    end: float  # wall-clock of the last kept frame


class RingBuffer:
    def __init__(self, max_seconds: float, margin: float = 30.0):
        self.max_seconds = max_seconds
        self.margin = margin
        self._segments: list[Segment] = []
        self._lock = threading.Lock()

    # --- fed by the recorder ------------------------------------------------

    def opened(self, path: str | Path, start: float) -> None:
        with self._lock:
            self._segments.append(Segment(Path(path), start))

    def closed(self, path: str | Path, end: float) -> None:
        path = Path(path)
        with self._lock:
            for seg in reversed(self._segments):
                if seg.path == path:
                    seg.end = end
                    break
        self.prune()

    def reset(self) -> None:
        """Forget everything (capture restarted: footage before the gap is not contiguous)."""
        with self._lock:
            doomed = [s for s in self._segments if not s.pins]
            self._segments = [s for s in self._segments if s.pins]
        for seg in doomed:
            _unlink(seg.path)

    # --- queries --------------------------------------------------------------

    def buffered_seconds(self) -> float:
        with self._lock:
            closed = [s for s in self._segments if s.closed]
            if not closed:
                return 0.0
            return min(closed[-1].end - closed[0].start, self.max_seconds)

    def latest_end(self) -> float | None:
        with self._lock:
            for seg in reversed(self._segments):
                if seg.closed:
                    return seg.end
        return None

    def select(self, since: float, until: float) -> Selection | None:
        """Closed segments covering [since, until]; pins them. Call release() after."""
        with self._lock:
            chosen = [s for s in self._segments if s.closed and s.end > since and s.start < until]
            if not chosen:
                return None
            for seg in chosen:
                seg.pins += 1
        start = max(since, chosen[0].start)
        end = min(until, chosen[-1].end)
        return Selection(chosen, offset=start - chosen[0].start, duration=end - start, start=start, end=end)

    def release(self, selection: Selection) -> None:
        with self._lock:
            for seg in selection.segments:
                seg.pins -= 1
        self.prune()

    # --- retention --------------------------------------------------------------

    def prune(self) -> None:
        with self._lock:
            newest = next((s.end for s in reversed(self._segments) if s.closed), None)
            if newest is None:
                return
            cutoff = newest - self.max_seconds - self.margin
            keep, doomed = [], []
            for seg in self._segments:
                if seg.closed and seg.end < cutoff and not seg.pins:
                    doomed.append(seg)
                else:
                    keep.append(seg)
            self._segments = keep
        for seg in doomed:
            _unlink(seg.path)


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
