"""What is in the clips folder: saved clips and screenshots, newest first.

Pure listing, no Qt and no GStreamer, so the gallery and tests can use it
anywhere. Clips are the ``*.mp4`` files directly in the output folder,
screenshots the ``*.png`` files directly in its ``Images`` subfolder (see
:mod:`momento.screenshot`). Nothing is searched recursively.

Temp files never show up: the exporter writes ``.<name>.tmp.mp4``,
``.<name>.part<N>.mp4`` and ``.<name>.segments.txt`` and screenshots go
through ``.momento-*.tmp``; every one of them starts with a dot, and dotfiles
are skipped. So is anything else with another extension.

Listing never raises: a missing folder is empty, and a file that vanishes
between the directory read and its ``stat`` (a clip being deleted or renamed
while the gallery refreshes) is simply left out.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CLIP_SUFFIX = ".mp4"
SHOT_SUFFIX = ".png"
KINDS = ("all", "clip", "shot")
PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class MediaItem:
    path: Path
    kind: str        # "clip" | "shot"
    mtime: float
    size: int


def _images_subdir() -> str:
    try:
        from .screenshot import IMAGES_DIR
        return IMAGES_DIR
    except Exception:  # keep listing usable even if that module can't import
        return "Images"


def images_dir(output_dir) -> Path:
    """Where the screenshots of ``output_dir`` (the clips folder) live."""
    return Path(output_dir).expanduser() / _images_subdir()


def _list(directory: Path, suffix: str, kind: str) -> list[MediaItem]:
    out = []
    try:
        it = os.scandir(directory)
    except OSError:  # missing, not a directory, no permission
        return out
    with it:
        for entry in it:
            name = entry.name
            if name.startswith(".") or not name.lower().endswith(suffix):
                continue
            try:
                if not entry.is_file():
                    continue
                st = entry.stat()
            except OSError:  # gone since the directory was read
                continue
            out.append(MediaItem(Path(entry.path), kind, st.st_mtime, st.st_size))
    return out


def scan(output_dir) -> list[MediaItem]:
    """Clips and screenshots under ``output_dir``, newest first (by mtime, then name)."""
    base = Path(output_dir).expanduser()
    items = _list(base, CLIP_SUFFIX, "clip") + _list(images_dir(base), SHOT_SUFFIX, "shot")
    items.sort(key=lambda i: (i.mtime, i.path.name), reverse=True)
    return items


def filter_items(items, kind: str) -> list[MediaItem]:
    """``kind`` "all" keeps everything; "clip" or "shot" only that kind."""
    if kind == "all":
        return list(items)
    if kind not in KINDS:
        raise ValueError(f"unknown media kind {kind!r}; use one of: {', '.join(KINDS)}")
    return [i for i in items if i.kind == kind]


def nearest(items, mtime: float) -> int:
    """Index of the item closest in time to ``mtime`` (the newer one on a tie); -1 if empty.

    Keeps your place when switching filters: look up the selected item's mtime
    in the newly filtered list.
    """
    best, best_d = -1, None
    for idx, item in enumerate(items):
        d = abs(item.mtime - mtime)
        if best_d is None or d < best_d or (d == best_d and item.mtime > items[best].mtime):
            best, best_d = idx, d
    return best


def clip_duration(path, timeout: float = PROBE_TIMEOUT) -> float | None:
    """Length of a clip in seconds (ffprobe reads the MP4 header), None if unknown."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        value = float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value >= 0 else None
