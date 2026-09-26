"""Disk-space rules: how much room a full replay buffer needs, and whether it fits.

Momento refuses to start capture when the disk holding the buffer can't fit a
full buffer for the chosen settings plus a reserve for the rest of the system.
All sizes are bytes; ``human`` formats them for people (decimal GB, like
file managers and ``quality.buffer_gb``).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

from . import quality

MUX_OVERHEAD = 1.05  # MPEG-TS packetisation + audio framing on top of the raw bitrates
RESERVE = 1 << 30  # 1 GiB left free for the system; the buffer never fills the disk
LOW_WATER = 512 << 20  # while recording: stop when free space drops below this
SAVE_MARGIN = 256 << 20  # a save needs its estimated size plus this much free
DEFAULT_AUDIO_KBPS = 160


def audio_kbps(cfg: dict) -> int:
    """AAC bitrate, or 0 when neither desktop sound nor the microphone is recorded.

    Desktop + mic are mixed into one stream, so enabling both costs the same as one.
    """
    a = cfg.get("audio") or {}
    if not (a.get("desktop") or a.get("microphone")):
        return 0
    return int(a.get("bitrate_kbps") or DEFAULT_AUDIO_KBPS)


def buffer_bytes(cfg: dict, source=None) -> int:
    """Disk used by a full ring buffer (video + audio, with mux overhead).

    ``source``: the recorded picture's size, when known; a resolution taller
    than it is counted at the size actually recorded (see quality.bitrate_kbps).
    """
    kbps = quality.bitrate_kbps(cfg["capture"], source) + audio_kbps(cfg)
    seconds = int(cfg["buffer"]["max_seconds"])
    return int(kbps * 1000 / 8 * seconds * MUX_OVERHEAD)


def required_bytes(cfg: dict, source=None) -> int:
    """Free space needed before capture may start: a full buffer + RESERVE."""
    return buffer_bytes(cfg, source) + RESERVE


def _existing(path) -> Path:
    p = Path(path).expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def free_bytes(path) -> int:
    """Bytes available to this user on the filesystem holding ``path`` (or its nearest existing parent)."""
    st = os.statvfs(_existing(path))
    return st.f_bavail * st.f_frsize


def dir_bytes(path) -> int:
    """Total size of the regular files directly inside ``path`` (0 if it doesn't exist)."""
    total = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for e in entries:
        try:
            if e.is_file(follow_symlinks=False):
                total += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return total


def buffer_dir(cfg: dict) -> str:
    return str(cfg["buffer"]["dir"])


def check(cfg: dict, reclaimable: int = 0, source=None) -> dict:
    """Would a full buffer for ``cfg`` fit?

    ``reclaimable`` is the size of our own current buffer segments, which a
    (re)start deletes before recording, so it counts as free. ``source`` is the
    recorded picture's size, when known (see buffer_bytes).
    """
    path = buffer_dir(cfg)
    free = free_bytes(path)
    required = required_bytes(cfg, source)
    reclaimable = max(0, int(reclaimable))
    return {"ok": free + reclaimable >= required, "free": free, "required": required,
            "reclaimable": reclaimable, "path": path}


def combo_key(resolution: str, quality_name: str, fps: int) -> str:
    return f"{resolution}/{quality_name}/{fps}"


def current_key(cfg: dict) -> str:
    cap = cfg["capture"]
    return combo_key(str(cap.get("resolution", quality.DEFAULT_RESOLUTION)).lower(),
                     str(cap.get("quality", quality.DEFAULT_QUALITY)).lower(),
                     quality.fps(cap))


def requirements(cfg: dict, reclaimable: int = 0, source=None) -> dict:
    """Required bytes for every resolution/quality/fps choice, for a settings UI.

    Everything else (audio, buffer length, an explicit bitrate) comes from ``cfg``.
    An option fits when ``free + reclaimable >= required[key]``. With a known
    ``source`` size, a resolution taller than it costs what is really recorded.
    """
    required = {}
    for res in quality.RESOLUTIONS:
        for q in quality.QUALITIES:
            for f in quality.FPS_CHOICES:
                c = copy.deepcopy(cfg)
                c["capture"].update(resolution=res, quality=q, fps=f)
                required[combo_key(res, q, f)] = required_bytes(c, source)
    path = buffer_dir(cfg)
    return {"required": required, "current": current_key(cfg), "free": free_bytes(path),
            "reclaimable": max(0, int(reclaimable)), "reserve": RESERVE, "path": path}


def label(cfg: dict) -> str:
    """'1440p Ultra', '1080p High 120 fps', '1080p at 50 Mbps' (explicit bitrate)."""
    cap = cfg["capture"]
    text = str(cap.get("resolution", quality.DEFAULT_RESOLUTION)).lower()
    explicit = int(cap.get("bitrate_kbps") or 0)
    if explicit > 0:
        text += f" at {explicit / 1000:g} Mbps"
    else:
        text += " " + str(cap.get("quality", quality.DEFAULT_QUALITY)).lower().capitalize()
    f = quality.fps(cap)
    if f != quality.FPS:
        text += f" {f} fps"
    return text


def human(n: float) -> str:
    """7200000000 -> '7.2 GB' (decimal units)."""
    n = float(n)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if abs(n) >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n / 1e3:.0f} kB" if abs(n) >= 1e3 else f"{int(n)} B"


def start_error(chk: dict) -> str:
    return (f"Not enough free space: needs {human(chk['required'])}, "
            f"{human(chk['free'] + chk['reclaimable'])} free")
