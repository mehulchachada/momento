"""Disk-space rules: how much room a full replay buffer needs, and whether it fits.

Momento refuses to start capture when the disk holding the buffer can't fit a
full buffer for the chosen settings plus a reserve for the rest of the system,
and warns ("low") when a full span of recording - with Keep history, plus the
span it saves to the output folder - doesn't fit.
All sizes are bytes; ``human`` formats them for people (decimal GB, like
file managers and ``quality.buffer_gb``).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

from . import durations, quality

MUX_OVERHEAD = 1.05  # MPEG-TS packetisation + audio framing on top of the raw bitrates
RESERVE = 1 << 30  # 1 GiB left free for the system; the buffer never fills the disk
LOW_WATER = 512 << 20  # while recording: stop when free space drops below this
SAVE_MARGIN = 256 << 20  # a save needs its estimated size plus this much free
REARM_MARGIN = 1 << 30  # a low-storage warning is sent again only after this much more than needed was free
DEFAULT_AUDIO_KBPS = 160


def audio_kbps(cfg: dict) -> int:
    """AAC bitrate, or 0 when neither desktop sound nor the microphone is recorded.

    Desktop + mic are mixed into one stream, so enabling both costs the same as one.
    """
    a = cfg.get("audio") or {}
    if not (a.get("desktop") or a.get("microphone")):
        return 0
    return int(a.get("bitrate_kbps") or DEFAULT_AUDIO_KBPS)


def buffer_bytes(cfg: dict, source=None, refresh=None) -> int:
    """Disk used by a full ring buffer (video + audio, with mux overhead).

    ``source``: the recorded picture's size, when known; a resolution taller
    than it is counted at the size actually recorded (see quality.bitrate_kbps).
    ``refresh``: the recorded screen's refresh rate (Hz), when known: fps "auto"
    is counted at the frame rate it records at (``quality.fps``).
    """
    kbps = quality.bitrate_kbps(cfg["capture"], source, refresh) + audio_kbps(cfg)
    seconds = int(cfg["buffer"]["max_seconds"])
    return int(kbps * 1000 / 8 * seconds * MUX_OVERHEAD)


def required_bytes(cfg: dict, source=None, refresh=None) -> int:
    """Free space needed before capture may start: a full buffer + RESERVE."""
    return buffer_bytes(cfg, source, refresh) + RESERVE


def keep_history(cfg: dict) -> bool:
    return bool((cfg.get("buffer") or {}).get("keep_history"))


def history_bytes(cfg: dict, source=None, refresh=None) -> int:
    """keep_history: room one saved span (the hour, at the default length) takes in
    the output folder. The export copies the footage, so about a full buffer; 0 when off."""
    return buffer_bytes(cfg, source, refresh) if keep_history(cfg) else 0


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


def output_dir(cfg: dict) -> str | None:
    out = (cfg.get("output") or {}).get("dir")
    return str(out) if out else None


def same_disk(a, b) -> bool:
    """Do ``a`` and ``b`` (or their nearest existing parents) live on one filesystem?"""
    try:
        return os.stat(_existing(a)).st_dev == os.stat(_existing(b)).st_dev
    except OSError:
        return True


def check(cfg: dict, reclaimable: int = 0, source=None, refresh=None) -> dict:
    """Would a full buffer for ``cfg`` fit, and is there room for a full span of recording?

    ``reclaimable`` is the size of our own current buffer segments, which a
    (re)start deletes before recording, so it counts as free. ``source`` is the
    recorded picture's size, when known: a resolution taller than it is counted
    (and labelled) at the size really recorded (see buffer_bytes, label).
    ``refresh`` is the recorded screen's refresh rate (Hz), when known: fps
    "auto" is counted (and labelled) at the frame rate it records at.

    ``ok`` answers the first question (capture may start). ``low`` answers the
    second: ``available`` < ``needed``, where needed is a full buffer + RESERVE,
    plus (keep_history) the span saved into the output folder. With the output
    folder on another disk, the saved span is checked against that disk instead,
    and ``disk`` says which one ``needed``/``available`` describe.
    """
    path = buffer_dir(cfg)
    free = free_bytes(path)
    required = required_bytes(cfg, source, refresh)
    reclaimable = max(0, int(reclaimable))
    room = free + reclaimable
    needed, available, disk, counted = required, room, "buffer", False
    history = history_bytes(cfg, source, refresh)
    out = output_dir(cfg)
    if history:
        if out is None or same_disk(path, out):
            needed, counted = needed + history, True
        elif room >= required:
            out_free = free_bytes(out)
            if out_free < history + RESERVE:
                needed, available, disk, counted = history + RESERVE, out_free, "output", True
    return {"ok": room >= required, "free": free, "required": required,
            "reclaimable": reclaimable, "path": path,
            "low": available < needed, "needed": needed, "available": available,
            "history": counted, "disk": disk, "label": label(cfg, source, refresh)}


def combo_key(resolution: str, quality_name: str, fps) -> str:
    """"1080p/high/60", "1080p/high/auto": one choice of resolution, quality and frame rate setting."""
    return f"{resolution}/{quality_name}/{fps}"


def current_key(cfg: dict) -> str:
    cap = cfg["capture"]
    return combo_key(quality.offered(cap.get("resolution", quality.DEFAULT_RESOLUTION)),
                     str(cap.get("quality", quality.DEFAULT_QUALITY)).lower(),
                     quality.fps_setting(cap))


def requirements(cfg: dict, reclaimable: int = 0, source=None, refresh=None) -> dict:
    """Required bytes for every resolution/quality/fps choice, for a settings UI.

    Everything else (audio, buffer length, an explicit bitrate) comes from ``cfg``.
    An option fits when ``free + reclaimable >= required[key]``. With a known
    ``source`` size, a resolution taller than it costs what is really recorded;
    fps "auto" costs what it records at on a screen of ``refresh`` Hz (60 fps's
    cost while that is unknown).
    """
    required = {}
    for res in quality.RESOLUTIONS:
        for q in quality.QUALITIES:
            for f in quality.FPS_CHOICES:
                c = copy.deepcopy(cfg)
                c["capture"].update(resolution=res, quality=q, fps=f)
                required[combo_key(res, q, f)] = required_bytes(c, source, refresh)
    path = buffer_dir(cfg)
    return {"required": required, "current": current_key(cfg), "free": free_bytes(path),
            "reclaimable": max(0, int(reclaimable)), "reserve": RESERVE, "path": path}


def label(cfg: dict, source=None, refresh=None) -> str:
    """'1080p Ultra', '1080p High 120 fps', '1080p at 50 Mbps' (explicit bitrate).

    With a known ``source`` size, a resolution taller than it is named by what is
    really recorded: 1080p on a 1280x720 window is '720p High'. An older config's
    1440p/2160p is named by what it records at ('1080p High'). The frame rate is
    the one recorded (fps "auto" on a 120 Hz screen: '120 fps'), named when not 60.
    """
    cap = cfg["capture"]
    text = quality.offered(cap.get("resolution", quality.DEFAULT_RESOLUTION))
    if quality.effective_resolution(text, source) != text:
        text = quality.height_label(source)
    explicit = int(cap.get("bitrate_kbps") or 0)
    if explicit > 0:
        text += f" at {explicit / 1000:g} Mbps"
    else:
        text += " " + str(cap.get("quality", quality.DEFAULT_QUALITY)).lower().capitalize()
    f = quality.fps(cap, refresh)
    if f != 60:
        text += f" {f} fps"
    return text


def human(n: float) -> str:
    """7200000000 -> '7.2 GB' (decimal units)."""
    n = float(n)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if abs(n) >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n / 1e3:.0f} kB" if abs(n) >= 1e3 else f"{int(n)} B"


def span(seconds: float) -> str:
    """3600 -> '60 min', 1800 -> '30 min', 90 -> '1m30s'."""
    seconds = int(round(seconds))
    return f"{seconds // 60} min" if seconds >= 60 and not seconds % 60 else durations.label(seconds)


def low_message(chk: dict, seconds: float) -> str:
    """'Low storage: 60 min at 1080p High needs 8.2 GB, 5.1 GB free. Free up space.'

    For a storage check with ``low`` set; ``seconds`` is the buffer length. Keep
    history is named only when its saved span is part of the need.
    """
    need, avail = human(chk.get("needed") or 0), human(chk.get("available") or 0)
    if chk.get("disk") == "output":
        return f"Low storage: Keep history needs {need} in the clips folder, {avail} free. Free up space."
    what = f"{span(seconds)} at {chk.get('label') or 'these settings'}"
    if chk.get("history"):
        what += " with Keep history"
    return f"Low storage: {what} needs {need}, {avail} free. Free up space."


def start_error(chk: dict) -> str:
    return (f"Not enough free space: needs {human(chk['required'])}, "
            f"{human(chk['free'] + chk['reclaimable'])} free")
